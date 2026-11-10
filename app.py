import os
import sys
import threading
import time
import re
from typing import Dict, Set
from flask import Flask, request, jsonify, send_from_directory

from core.storage import (
    load_settings, save_settings,
    list_sessions, get_session, create_session, save_session, delete_session,
    is_valid_session_id
)
from core.engine import MeetingEngine, generate_msg_id
from core.executor import Executor, validate_cli_template
from core.scanner import scan_local_models

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(__name__, static_folder=STATIC_DIR)

# ----------------- 并发线程管理器 -----------------
_RUNNER_LOCK = threading.Lock()
_ACTIVE_SESSIONS: Set[str] = set()

def start_background_runner(session_id: str):
    """
    单会话并发互斥启动器：确保每个 session_id 永远只有一个后台线程在推进，避免双写覆盖。
    """
    with _RUNNER_LOCK:
        if session_id in _ACTIVE_SESSIONS:
            return  # 已经在运行中，不重复创建
        _ACTIVE_SESSIONS.add(session_id)

    def _worker():
        try:
            while True:
                session = get_session(session_id)
                if not session or session.get("status") != "running":
                    break

                has_acted, desc = MeetingEngine.step(session)
                if not has_acted:
                    break

                time.sleep(0.3)
        except Exception as ex:
            print(f"[MeetingRunner] 异常捕获 (Session: {session_id}):", ex)
            try:
                curr_s = get_session(session_id)
                if curr_s:
                    curr_s["status"] = "paused"
                    curr_s["current_speaker"] = None
                    curr_s["messages"].append({
                        "id": generate_msg_id(),
                        "sender": "system",
                        "sender_name": "系统异常捕获",
                        "content": f"⚠️ 会议推进遇到内部异常: {str(ex)}。已自动暂停，可点击恢复继续。",
                        "phase": curr_s.get("phase", 1),
                        "verdict": "ERROR",
                        "elapsed_time": 0,
                        "created_at": int(time.time()),
                        "sent_prompt": "",
                        "raw_response": str(ex)
                    })
                    save_session(curr_s)
            except Exception as e_save:
                print("[MeetingRunner] 异常恢复保存失败:", e_save)
        finally:
            with _RUNNER_LOCK:
                _ACTIVE_SESSIONS.discard(session_id)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

# ----------------- 路径安全过滤助手 -----------------
SENSITIVE_PATTERNS = [
    re.compile(r"(^|[/\\])\.ssh([/\\]|$)"),
    re.compile(r"(^|[/\\])\.gnupg([/\\]|$)"),
    re.compile(r"(^|[/\\])\.aws([/\\]|$)"),
    re.compile(r"(^|[/\\])\.azure([/\\]|$)"),
    re.compile(r"(^|[/\\])\.kube([/\\]|$)"),
    re.compile(r"(^|[/\\])\.env($|\.)"),
    re.compile(r"(^|[/\\])\.bash_history($|\.)"),
    re.compile(r"(^|[/\\])\.zsh_history($|\.)"),
    re.compile(r"(^|[/\\])(id_rsa|id_ed25519|credentials|token)"),
]

def is_path_safe(target_path: str) -> bool:
    """严格校验路径安全性，杜绝路径穿越、系统文件泄露与敏感凭据窃取"""
    if not target_path or not isinstance(target_path, str):
        return False

    real_path = os.path.realpath(os.path.expanduser(target_path.strip()))
    user_home = os.path.realpath(os.path.expanduser("~"))
    cwd = os.path.realpath(os.getcwd())

    # 路径必须位于当前工作空间内或用户家目录内，严禁读取系统目录 (/etc, /private, /proc 等)
    in_home = real_path == user_home or real_path.startswith(user_home + os.sep)
    in_cwd = real_path == cwd or real_path.startswith(cwd + os.sep)
    if not (in_home or in_cwd):
        return False

    for pat in SENSITIVE_PATTERNS:
        if pat.search(real_path):
            return False

    return True

def sanitize_sessions_on_startup():
    """服务器启动时自愈：清除上次异常崩溃或 Ctrl+C 残留的挂起状态"""
    try:
        sessions = list_sessions()
        now = int(time.time())
        for s in sessions:
            full_s = get_session(s["id"])
            if full_s:
                changed = False
                if full_s.get("current_speaker"):
                    full_s["current_speaker"] = None
                    changed = True
                if full_s.get("status") == "running":
                    full_s["status"] = "paused"
                    full_s["messages"].append({
                        "id": generate_msg_id(),
                        "sender": "system",
                        "sender_name": "系统复位",
                        "content": "⚠️ 服务端已重启，系统已复位挂起的发言状态，会议转为暂停，等待群主人工恢复。",
                        "phase": full_s.get("phase", 1),
                        "verdict": "RECOVER",
                        "elapsed_time": 0,
                        "created_at": now,
                        "sent_prompt": "",
                        "raw_response": ""
                    })
                    changed = True
                if changed:
                    save_session(full_s)
    except Exception as e:
        print("启动复位清理异常:", e)

# 仅在实际 Worker 进程中执行一次自愈清理
if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
    sanitize_sessions_on_startup()

# ----------------- 静态页面路由 -----------------
@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")

@app.route("/static/<path:path>")
def serve_static(path):
    return send_from_directory(STATIC_DIR, path)

# ----------------- Scanner & Settings API -----------------
@app.route("/api/default_workspace", methods=["GET"])
def api_default_workspace():
    """获取服务端推荐的初始安全工作目录"""
    default_dir = os.getcwd()
    return jsonify({"workspace_dir": default_dir})

@app.route("/api/scan_models", methods=["GET"])
def api_scan_models():
    force = request.args.get("refresh", "0") in ["1", "true", "True"]
    models = scan_local_models(force_refresh=force)
    return jsonify({"models": models})

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify(load_settings())

@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    data = request.json or {}
    
    # 严格校验配置 Schema 与 CLI 命令合法性
    moderator = data.get("moderator")
    if not isinstance(moderator, dict) or not moderator.get("cli_cmd"):
        return jsonify({"error": "主持人配置无效或缺少 cli_cmd"}), 400
    
    ok, err = validate_cli_template(moderator["cli_cmd"])
    if not ok:
        return jsonify({"error": f"主持人 CLI 命令不合法: {err}"}), 400

    employees = data.get("employees", [])
    if not isinstance(employees, list):
        return jsonify({"error": "employees 必须为列表"}), 400

    for idx, emp in enumerate(employees):
        if not isinstance(emp, dict) or not emp.get("cli_cmd"):
            return jsonify({"error": f"第 {idx+1} 个员工配置缺少 cli_cmd"}), 400
        ok, err = validate_cli_template(emp["cli_cmd"])
        if not ok:
            return jsonify({"error": f"员工 [{emp.get('name', idx)}] CLI 命令不合法: {err}"}), 400

    save_settings(data)
    return jsonify({"success": True, "settings": data})

# ----------------- Sessions API -----------------
@app.route("/api/sessions", methods=["GET"])
def api_list_sessions():
    return jsonify(list_sessions())

@app.route("/api/sessions", methods=["POST"])
def api_create_session():
    data = request.json or {}
    title = (data.get("title") or "新方案研讨会议").strip()
    employee_ids = data.get("employee_ids")
    moderator_id = data.get("moderator_id")
    workspace_dir = data.get("workspace_dir")

    if workspace_dir and not is_path_safe(workspace_dir):
        workspace_dir = os.getcwd()

    session = create_session(
        title=title,
        employee_ids=employee_ids,
        moderator_id=moderator_id,
        workspace_dir=workspace_dir
    )
    return jsonify(session)

@app.route("/api/sessions/<session_id>", methods=["GET"])
def api_get_session(session_id):
    if not is_valid_session_id(session_id):
        return jsonify({"error": "非法 session_id"}), 400

    session = get_session(session_id)
    if not session:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify(session)

@app.route("/api/sessions/<session_id>", methods=["PUT"])
def api_update_session(session_id):
    if not is_valid_session_id(session_id):
        return jsonify({"error": "非法 session_id"}), 400

    session = get_session(session_id)
    if not session:
        return jsonify({"error": "会话不存在"}), 404
    
    data = request.json or {}
    if "title" in data:
        session["title"] = str(data["title"]).strip() or session["title"]
    if "is_favorite" in data:
        session["is_favorite"] = bool(data["is_favorite"])
    if "employee_ids" in data and isinstance(data["employee_ids"], list):
        old_ids = session.get("employee_ids", [])
        new_ids = data["employee_ids"]
        if set(old_ids) != set(new_ids):
            added_ids = [x for x in new_ids if x not in old_ids]
            removed_ids = [x for x in old_ids if x not in new_ids]
            settings = load_settings()
            parts = []
            if added_ids:
                added_names = [MeetingEngine.get_employee(settings, eid)["name"] for eid in added_ids]
                parts.append(f"加入了 [{', '.join(added_names)}]")
            if removed_ids:
                removed_names = [MeetingEngine.get_employee(settings, eid)["name"] for eid in removed_ids]
                parts.append(f"移出了 [{', '.join(removed_names)}]")
            
            notice_text = f"📢 参会人员变动：{'；'.join(parts)}"
            now = int(time.time())
            session["messages"].append({
                "id": generate_msg_id(),
                "sender": "system",
                "sender_name": "系统广播",
                "content": notice_text,
                "phase": session.get("phase", 1),
                "verdict": "MEMBER_CHANGE",
                "elapsed_time": 0,
                "created_at": now,
                "sent_prompt": "",
                "raw_response": ""
            })
            session["employee_ids"] = new_ids
    if "workspace_dir" in data:
        raw_dir = str(data["workspace_dir"]).strip()
        if is_path_safe(raw_dir) and os.path.exists(raw_dir):
            session["workspace_dir"] = os.path.abspath(raw_dir)
    if "document" in data:
        session["document"] = str(data["document"])
        
    save_session(session)
    return jsonify(session)

@app.route("/api/sessions/<session_id>", methods=["DELETE"])
def api_delete_session(session_id):
    if not is_valid_session_id(session_id):
        return jsonify({"error": "非法 session_id"}), 400

    Executor.kill_session_process(session_id)
    success = delete_session(session_id)
    return jsonify({"success": success})

# ----------------- Workspace Explorer & Folder Picker API -----------------
@app.route("/api/browse_dirs", methods=["GET"])
def api_browse_dirs():
    """本地安全文件夹浏览选择器"""
    target = request.args.get("path", "").strip()
    user_home = os.path.expanduser("~")
    
    if not target or not is_path_safe(target):
        target = user_home

    target = os.path.realpath(os.path.expanduser(target))
    if not os.path.exists(target) or not os.path.isdir(target):
        target = user_home

    # 严禁逃逸至根目录
    if target == "/" or not is_path_safe(target):
        target = user_home

    dirs = []
    try:
        for entry in os.scandir(target):
            if entry.name.startswith(".") or entry.name in ["Library", "node_modules", ".git", "__pycache__", "venv"]:
                continue
            if entry.is_dir() and is_path_safe(entry.path):
                dirs.append({
                    "name": entry.name,
                    "path": entry.path
                })
        dirs.sort(key=lambda x: x["name"].lower())
        parent = os.path.dirname(target) if target not in ["/", user_home] else None
        return jsonify({
            "current": target,
            "parent": parent,
            "dirs": dirs
        })
    except Exception as e:
        return jsonify({"current": target, "parent": None, "dirs": [], "error": str(e)})

@app.route("/api/workspace/files", methods=["GET"])
def api_workspace_files():
    path = request.args.get("path", "").strip()
    if not path or not is_path_safe(path) or not os.path.exists(path):
        return jsonify({"error": "非法或不存在的路径", "items": []})
    
    items = []
    try:
        for entry in os.scandir(path):
            if entry.name.startswith(".") or entry.name in ["node_modules", ".git", "__pycache__", "venv"]:
                continue
            if not is_path_safe(entry.path):
                continue
            items.append({
                "name": entry.name,
                "path": entry.path,
                "is_dir": entry.is_dir(),
                "size": entry.stat().st_size if entry.is_file() else 0
            })
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return jsonify({"path": path, "items": items})
    except Exception as e:
        return jsonify({"error": str(e), "items": []})

@app.route("/api/workspace/file", methods=["GET"])
def api_workspace_file_content():
    fpath = request.args.get("path", "").strip()
    if not fpath or not is_path_safe(fpath) or not os.path.exists(fpath) or not os.path.isfile(fpath):
        return jsonify({"error": "文件不存在或无权访问"}), 404
    
    if os.path.getsize(fpath) > 500 * 1024:
        return jsonify({"content": "【文件过大 (>500KB)，不支持在线快速预览】", "path": fpath})
    
    try:
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return jsonify({"content": content, "path": fpath, "name": os.path.basename(fpath)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ----------------- Meeting Controls API -----------------
@app.route("/api/sessions/<session_id>/message", methods=["POST"])
def api_send_message(session_id):
    if not is_valid_session_id(session_id):
        return jsonify({"error": "非法 session_id"}), 400

    session = get_session(session_id)
    if not session:
        return jsonify({"error": "会话不存在"}), 404
    
    data = request.json or {}
    content = str(data.get("content", "")).strip()
    if not content:
        return jsonify({"error": "发言内容不能为空"}), 400
    
    MeetingEngine.user_inject(session, content)
    start_background_runner(session_id)
    
    return jsonify(get_session(session_id))

@app.route("/api/sessions/<session_id>/pause", methods=["POST"])
def api_pause(session_id):
    if not is_valid_session_id(session_id):
        return jsonify({"error": "非法 session_id"}), 400

    session = get_session(session_id)
    if not session:
        return jsonify({"error": "会话不存在"}), 404
    
    session["status"] = "paused"
    session["current_speaker"] = None
    save_session(session)

    killed = Executor.kill_session_process(session_id)
    
    now = int(time.time())
    session["messages"].append({
        "id": generate_msg_id(),
        "sender": "system",
        "sender_name": "系统广播",
        "content": "🛑 会议已被群主手动暂停，正在运行的 AI 任务已立即终止中断。",
        "phase": session.get("phase", 1),
        "verdict": "PAUSE",
        "elapsed_time": 0,
        "created_at": now,
        "sent_prompt": "",
        "raw_response": ""
    })
    save_session(session)
    
    return jsonify({"success": True, "killed": killed, "status": "paused"})

@app.route("/api/sessions/<session_id>/resume", methods=["POST"])
def api_resume(session_id):
    if not is_valid_session_id(session_id):
        return jsonify({"error": "非法 session_id"}), 400

    session = get_session(session_id)
    if not session:
        return jsonify({"error": "会话不存在"}), 404
    
    session["status"] = "running"
    save_session(session)
    
    start_background_runner(session_id)
    return jsonify({"success": True, "status": "running"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    host = os.environ.get("HOST", "127.0.0.1")  # 默认仅绑定本地，杜绝局域网调试器暴露
    is_debug = os.environ.get("FLASK_DEBUG", "1") in ["1", "true"]

    print(f"\n🚀 AI-Meeting (AI 圆桌会议室) 启动中...")
    print(f"👉 访问地址: http://127.0.0.1:{port}")
    if is_debug:
        print(f"⚡ 代码热重载已启用 (无交互调试器暴露，安全模式)")

    # use_debugger=False 彻底禁止外部或局域网访问 Werkzeug Debugger PIN 执行任意代码
    app.run(host=host, port=port, debug=is_debug, use_reloader=is_debug, use_debugger=False)
