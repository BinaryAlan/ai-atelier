import os
import json
import uuid
import time
import re
import threading
from typing import List, Dict, Any, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

os.makedirs(SESSIONS_DIR, exist_ok=True)

_SESSION_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_STORAGE_LOCK = threading.Lock()

def is_valid_session_id(session_id: str) -> bool:
    return bool(session_id and _SESSION_ID_REGEX.match(session_id))

DEFAULT_SETTINGS = {
    "moderator": {
        "id": "moderator",
        "name": "Antigravity",
        "avatar": "👑",
        "avatar_color": "#d97706",
        "type": "cli",
        "cli_cmd": 'agy -p "{prompt}"',
        "system_prompt": "你是会议主持人。负责中立客观地主持会议进程、梳理记录各方发言要点、综合整理方案纪要，并在全员达成共识时出具定稿报告。",
        "timeout": 180
    },
    "employees": [
        {
            "id": "emp_agy",
            "name": "agy",
            "avatar": "🦉",
            "avatar_color": "#2563eb",
            "type": "cli",
            "cli_cmd": 'agy -p "{prompt}"',
            "system_prompt": "",
            "enabled": True,
            "timeout": 180
        },
        {
            "id": "codebuddy-ds4",
            "name": "DeepSeek-V4 (DS4)",
            "avatar": "🧠",
            "avatar_color": "#9333ea",
            "type": "cli",
            "cli_cmd": 'codebuddy --model deepseek-v4-pro --tools "" -p "{prompt}"',
            "system_prompt": "擅长深度逻辑推演、推演潜在隐患与方案边界风险，以客观严谨的态度提出建设性意见。",
            "enabled": True,
            "timeout": 180
        },
        {
            "id": "codebuddy-hy4",
            "name": "混元-4 (HY4)",
            "avatar": "⚡",
            "avatar_color": "#059669",
            "type": "cli",
            "cli_cmd": 'codebuddy --model hy4std --tools "" -p "{prompt}"',
            "system_prompt": "注重实际落地执行可行性、周期效率与实操适配度，提供稳健务实的路径建议。",
            "enabled": True,
            "timeout": 120
        }
    ]
}

def load_settings() -> Dict[str, Any]:
    with _STORAGE_LOCK:
        if not os.path.exists(SETTINGS_FILE):
            save_settings(DEFAULT_SETTINGS)
            return DEFAULT_SETTINGS
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "moderator" not in data or not isinstance(data["moderator"], dict):
                    data["moderator"] = DEFAULT_SETTINGS["moderator"]
                    save_settings(data)
                return data
        except Exception:
            return DEFAULT_SETTINGS

def save_settings(settings: Dict[str, Any]):
    with _STORAGE_LOCK:
        tmp_path = f"{SETTINGS_FILE}.tmp.{uuid.uuid4().hex[:6]}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, SETTINGS_FILE)

def list_sessions() -> List[Dict[str, Any]]:
    sessions = []
    if not os.path.exists(SESSIONS_DIR):
        return sessions

    with _STORAGE_LOCK:
        fnames = [f for f in os.listdir(SESSIONS_DIR) if f.endswith(".json")]

    for fname in fnames:
        fpath = os.path.join(SESSIONS_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
                sessions.append({
                    "id": data.get("id"),
                    "title": data.get("title", "未命名会议"),
                    "is_favorite": data.get("is_favorite", False),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                    "status": data.get("status", "idle"),
                    "phase": data.get("phase", 1),
                    "employee_ids": data.get("employee_ids", []),
                    "workspace_dir": data.get("workspace_dir", "")
                })
        except Exception:
            continue

    sessions.sort(key=lambda s: (not s.get("is_favorite", False), -s.get("updated_at", 0)))
    return sessions

def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    if not is_valid_session_id(session_id):
        return None

    fpath = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    if not os.path.exists(fpath):
        return None

    # 读取重试机制（防止极高频并发原子写入瞬间读取失败）
    for attempt in range(2):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            time.sleep(0.05)
    return None

def create_session(
    title: str = "新方案研讨会议",
    employee_ids: Optional[List[str]] = None,
    moderator_id: Optional[str] = None,
    workspace_dir: Optional[str] = None
) -> Dict[str, Any]:
    session_id = str(uuid.uuid4())[:8]
    now = int(time.time())
    settings = load_settings()

    if employee_ids is None:
        employee_ids = [e["id"] for e in settings.get("employees", []) if e.get("enabled", True)]

    # 默认工作目录：使用当前系统的工作根目录
    if not workspace_dir or not os.path.exists(workspace_dir):
        workspace_dir = os.getcwd()

    session_data = {
        "id": session_id,
        "title": title or "新方案研讨会议",
        "is_favorite": False,
        "created_at": now,
        "updated_at": now,
        "status": "idle",  # idle | running | paused | concluded
        "phase": 1,        # 1: 自由立论; 2: 方案审阅
        "employee_ids": employee_ids,
        "workspace_dir": os.path.abspath(workspace_dir),
        "current_speaker": None,
        "document": "# 会议方案草案\n\n*(等待各参会成员发表初步立论中...)*\n",
        "blocker_under_review": None,
        "blocker_retry_count": 0,
        "passed_employees": [],
        "messages": []
    }
    save_session(session_data)
    return session_data

def save_session(session_data: Dict[str, Any]):
    session_id = session_data.get("id")
    if not is_valid_session_id(session_id):
        raise ValueError(f"非法或不安全的 session_id: {session_id}")

    session_data["updated_at"] = int(time.time())
    fpath = os.path.join(SESSIONS_DIR, f"{session_id}.json")

    with _STORAGE_LOCK:
        # 原子写入：写入随机临时文件后原子重命名替换
        tmp_path = f"{fpath}.tmp.{uuid.uuid4().hex[:6]}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(session_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, fpath)

def delete_session(session_id: str) -> bool:
    if not is_valid_session_id(session_id):
        return False

    fpath = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    with _STORAGE_LOCK:
        if os.path.exists(fpath):
            try:
                os.remove(fpath)
                return True
            except Exception:
                return False
    return False
