import os
import json
import copy
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

# 必须是可重入锁：load_settings() 在持锁状态下会再调用 save_settings()，
# 普通 threading.Lock 会造成同线程自我死锁（进而阻塞所有会话读写）。
_STORAGE_LOCK = threading.RLock()

# 会话级锁：串行化同一会话的「读-改-写」，避免旧副本覆盖新写入
_SESSION_LOCKS: Dict[str, threading.RLock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()


class SessionDeletedError(Exception):
    """后台推进过程中会话文件已被删除，调用方应立即停止推进。"""


def is_valid_session_id(session_id: Any) -> bool:
    return bool(isinstance(session_id, str) and session_id and _SESSION_ID_REGEX.match(session_id))


def session_lock(session_id: str) -> threading.RLock:
    """按会话粒度取锁（可重入），用于跨线程的读-改-写事务。"""
    with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is None:
            lock = threading.RLock()
            _SESSION_LOCKS[session_id] = lock
        return lock


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


def _read_json(path: str) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_settings() -> Dict[str, Any]:
    with _STORAGE_LOCK:
        if not os.path.exists(SETTINGS_FILE):
            defaults = copy.deepcopy(DEFAULT_SETTINGS)
            save_settings(defaults)
            return defaults

        data = _read_json(SETTINGS_FILE)
        if not isinstance(data, dict):
            # 配置文件损坏时回退默认值，但不覆盖用户文件（保留现场便于排查）
            return copy.deepcopy(DEFAULT_SETTINGS)

        changed = False
        if not isinstance(data.get("moderator"), dict):
            data["moderator"] = copy.deepcopy(DEFAULT_SETTINGS["moderator"])
            changed = True
        if not isinstance(data.get("employees"), list):
            data["employees"] = copy.deepcopy(DEFAULT_SETTINGS["employees"])
            changed = True
        if changed:
            save_settings(data)
        return data


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
        data = _read_json(fpath)
        if not isinstance(data, dict):
            continue
        sessions.append({
            "id": data.get("id") or fname[:-5],
            "title": data.get("title") or "未命名会议",
            "is_favorite": bool(data.get("is_favorite", False)),
            "created_at": data.get("created_at") or 0,
            "updated_at": data.get("updated_at") or 0,
            "status": data.get("status", "idle"),
            "phase": data.get("phase", 1),
            "employee_ids": data.get("employee_ids", []),
            "workspace_dir": data.get("workspace_dir", "")
        })

    # 收藏优先，其次按更新时间倒序；updated_at 缺失/为 None 时按 0 处理，避免 TypeError
    sessions.sort(key=lambda s: (not s.get("is_favorite", False), -(s.get("updated_at") or 0)))
    return sessions


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    if not is_valid_session_id(session_id):
        return None

    fpath = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    if not os.path.exists(fpath):
        return None

    # 读取重试机制（防止极高频并发原子写入瞬间读取失败）
    for _ in range(2):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            time.sleep(0.05)
    return None


def create_session(
    title: str = "新方案研讨会议",
    employee_ids: Optional[List[str]] = None,
    workspace_dir: Optional[str] = None
) -> Dict[str, Any]:
    settings = load_settings()
    now = int(time.time())

    if employee_ids is None:
        employee_ids = [e["id"] for e in settings.get("employees", []) if e.get("enabled", True)]

    # 默认工作目录：使用当前系统的工作根目录
    if not workspace_dir or not os.path.isdir(workspace_dir):
        workspace_dir = os.getcwd()

    # 生成不冲突的会话 id（短 id 便于阅读，但先做存在性检查）
    session_id = ""
    for _ in range(20):
        candidate = uuid.uuid4().hex[:8]
        if not os.path.exists(os.path.join(SESSIONS_DIR, f"{candidate}.json")):
            session_id = candidate
            break
    if not session_id:
        session_id = uuid.uuid4().hex[:16]

    session_data = {
        "id": session_id,
        "title": title or "新方案研讨会议",
        "is_favorite": False,
        "created_at": now,
        "updated_at": now,
        "status": "idle",  # idle | running | paused
        "phase": 1,        # 1: 自由立论; 2: 方案审阅
        "employee_ids": employee_ids,
        "workspace_dir": os.path.abspath(workspace_dir),
        "current_speaker": None,
        "document": "# 会议方案草案\n\n*(等待各参会成员发表初步立论中...)*\n",
        "doc_revision": 0,   # 正文版本号，同时用作「同一轮审阅」的界线
        "blocker_under_review": None,
        "blocker_retry_count": 0,
        "review_comment_retries": {},
        "passed_employees": [],
        "skipped_employees": [],   # 被群主跳过的成员（CLI 报错后不再占用发言位）
        "messages": []
    }
    save_session(session_data)
    return session_data


def save_session(session_data: Dict[str, Any], merge: bool = False):
    """
    保存会话。

    merge=False：直接原子覆盖（API 层在 session_lock 保护下使用）。
    merge=True ：以磁盘最新状态为基线合并（后台线程在长时间 CLI 调用后回写时使用），
                 避免旧副本覆盖期间发生的暂停、成员变更、系统广播等写入。
                 会话文件已被删除时抛 SessionDeletedError，调用方须停止推进。
    """
    session_id = session_data.get("id")
    if not is_valid_session_id(session_id):
        raise ValueError(f"非法或不安全的 session_id: {session_id}")

    fpath = os.path.join(SESSIONS_DIR, f"{session_id}.json")

    with _STORAGE_LOCK:
        if merge:
            base = get_session(session_id)
            if base is None:
                raise SessionDeletedError(session_id)

            # 1) 合并消息：保留基线里本线程不知道的消息（系统广播、群主插话等）
            base_msgs = [m for m in (base.get("messages") or []) if isinstance(m, dict)]
            known_ids = {m.get("id") for m in base_msgs}
            merged_msgs = list(base_msgs)
            for msg in session_data.get("messages") or []:
                if isinstance(msg, dict) and msg.get("id") not in known_ids:
                    merged_msgs.append(msg)
                    known_ids.add(msg.get("id"))

            stale_status = session_data.get("status")
            latest_status = base.get("status")

            for key, value in session_data.items():
                if key == "messages":
                    continue
                base[key] = value
            base["messages"] = merged_msgs

            # 2) 用户暂停优先级最高：后台线程不得把它写回 running
            if latest_status == "paused" and stale_status == "running":
                base["status"] = "paused"
                base["current_speaker"] = None

            session_data = base

        session_data["updated_at"] = int(time.time())
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
