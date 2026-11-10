import os
import json
import uuid
import time
from typing import List, Dict, Any, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

os.makedirs(SESSIONS_DIR, exist_ok=True)

DEFAULT_SETTINGS = {
    "moderator": {
        "id": "moderator",
        "name": "Antigravity (首席主持)",
        "title": "会议主持人兼总架构师",
        "avatar": "👑",
        "avatar_color": "#d97706",
        "type": "cli",
        "cli_cmd": 'agy -p "{prompt}"',
        "system_prompt": "你是会议主持人兼全局首席架构师。注重工程规范、模块解耦、长远扩展性与高可靠性。负责综合各方立论生成方案草案、协调分歧并在结案时输出权威定稿。",
        "timeout": 180
    },
    "employees": [
        {
            "id": "emp_ds4",
            "name": "DeepSeek-V4 (代码挑刺官)",
            "title": "底层算法与代码挑刺官",
            "avatar": "🧠",
            "avatar_color": "#9333ea",
            "type": "cli",
            "cli_cmd": 'codebuddy --model deepseek-v4-pro --tools "" -p "{prompt}"',
            "system_prompt": "你是技术评审团的代码与性能挑刺官。敏锐寻找高并发死锁、内存隐患、边界漏洞与安全陷阱，严禁客套。",
            "enabled": True,
            "timeout": 180
        },
        {
            "id": "emp_hy4",
            "name": "混元-4 (业务落地专家)",
            "title": "业务落地与工程效率专家",
            "avatar": "⚡",
            "avatar_color": "#059669",
            "type": "cli",
            "cli_cmd": 'codebuddy --model hy4std --tools "" -p "{prompt}"',
            "system_prompt": "你是技术评审团的业务落地与效率专家。注重开发周期、团队维护成本、中文业务适配度与工程可行性。",
            "enabled": True,
            "timeout": 120
        }
    ]
}

def load_settings() -> Dict[str, Any]:
    if not os.path.exists(SETTINGS_FILE):
        save_settings(DEFAULT_SETTINGS)
        return DEFAULT_SETTINGS
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # 自动迁移旧版结构：若无单独 moderator 配置，从旧数据提取或使用默认
            if "moderator" not in data or not isinstance(data["moderator"], dict):
                data["moderator"] = DEFAULT_SETTINGS["moderator"]
                save_settings(data)
            return data
    except Exception:
        return DEFAULT_SETTINGS

def save_settings(settings: Dict[str, Any]):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)

def list_sessions() -> List[Dict[str, Any]]:
    sessions = []
    if not os.path.exists(SESSIONS_DIR):
        return sessions
    for fname in os.listdir(SESSIONS_DIR):
        if fname.endswith(".json"):
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
    fpath = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    if not os.path.exists(fpath):
        return None
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def create_session(
    title: str = "新架构评审会议",
    employee_ids: List[str] = None,
    moderator_id: str = None,
    workspace_dir: str = None
) -> Dict[str, Any]:
    session_id = str(uuid.uuid4())[:8]
    now = int(time.time())
    settings = load_settings()

    if not employee_ids:
        employee_ids = [e["id"] for e in settings.get("employees", []) if e.get("enabled", True)]

    # 默认工作目录
    if not workspace_dir:
        workspace_dir = os.path.expanduser("~/Code")
        if not os.path.exists(workspace_dir):
            workspace_dir = os.getcwd()

    session_data = {
        "id": session_id,
        "title": title,
        "is_favorite": False,
        "created_at": now,
        "updated_at": now,
        "status": "idle",  # idle | running | paused | concluded
        "phase": 1,        # 1: 自由立论; 2: 严格审查
        "employee_ids": employee_ids,
        "workspace_dir": os.path.abspath(workspace_dir),
        "current_speaker": None,
        "document": "# 会议技术方案草案\n\n*(等待各参会专家发表初步立论中...)*\n",
        "blocker_under_review": None,
        "passed_employees": [],
        "messages": []
    }
    save_session(session_data)
    return session_data

def save_session(session_data: Dict[str, Any]):
    session_data["updated_at"] = int(time.time())
    fpath = os.path.join(SESSIONS_DIR, f"{session_data['id']}.json")
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(session_data, f, ensure_ascii=False, indent=2)

def delete_session(session_id: str) -> bool:
    fpath = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    if os.path.exists(fpath):
        os.remove(fpath)
        return True
    return False
