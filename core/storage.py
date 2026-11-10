import os
import json
import uuid
import time
from typing import List, Dict, Any, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

# 确保目录存在
os.makedirs(SESSIONS_DIR, exist_ok=True)

DEFAULT_SETTINGS = {
    "moderator_id": "antigravity",
    "employees": [
        {
            "id": "antigravity",
            "name": "Antigravity",
            "title": "全局首席架构师",
            "avatar_color": "#2563eb",  # 蓝
            "type": "cli",
            "cli_cmd": "agy -p \"{prompt}\"",
            "system_prompt": "你是 Google Antigravity，技术评审团的全局首席架构师。注重工程规范、模块解耦、长远扩展性与高可靠性。",
            "enabled": True
        },
        {
            "id": "codebuddy-ds4",
            "name": "DeepSeek-V4 (DS4)",
            "title": "底层算法与代码挑刺官",
            "avatar_color": "#9333ea",  # 紫
            "type": "cli",
            "cli_cmd": "codebuddy --model deepseek-v4-pro --tools \"\" -p \"{prompt}\"",
            "system_prompt": "你是 DeepSeek V4，技术评审团的代码与性能挑刺官。敏锐寻找高并发死锁、内存隐患、边界漏洞与安全陷阱，严禁客套。",
            "enabled": True
        },
        {
            "id": "codebuddy-hy4",
            "name": "混元-4 (HY4)",
            "title": "业务落地与成本把控专家",
            "avatar_color": "#059669",  # 绿
            "type": "cli",
            "cli_cmd": "codebuddy --model hy4std --tools \"\" -p \"{prompt}\"",
            "system_prompt": "你是腾讯混元4，技术评审团的业务落地与效率专家。注重开发周期、团队维护成本、中文业务适配度与工程可行性。",
            "enabled": True
        }
    ]
}

def load_settings() -> Dict[str, Any]:
    if not os.path.exists(SETTINGS_FILE):
        save_settings(DEFAULT_SETTINGS)
        return DEFAULT_SETTINGS
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
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
                        "employee_ids": data.get("employee_ids", [])
                    })
            except Exception:
                continue
    # 收藏置顶，其次按更新时间倒序
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

def create_session(title: str = "新架构评审会议", employee_ids: List[str] = None) -> Dict[str, Any]:
    session_id = str(uuid.uuid4())[:8]
    now = int(time.time())
    if not employee_ids:
        settings = load_settings()
        employee_ids = [e["id"] for e in settings["employees"] if e.get("enabled", True)]

    session_data = {
        "id": session_id,
        "title": title,
        "is_favorite": False,
        "created_at": now,
        "updated_at": now,
        "status": "idle",  # idle | running | paused | concluded
        "phase": 1,        # 1: 自由立论与出谋划策; 2: 严格审查与重大阻断
        "employee_ids": employee_ids,
        "moderator_id": "antigravity",
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
