import os
import threading
import time
from flask import Flask, request, jsonify, send_from_directory, Response
from core.storage import (
    load_settings, save_settings,
    list_sessions, get_session, create_session, save_session, delete_session
)
from core.engine import MeetingEngine
from core.executor import Executor
from core.scanner import scan_local_models

app = Flask(__name__, static_folder="static")

def background_runner(session_id: str):
    """后台持续推进会议，直到暂停或结束"""
    while True:
        session = get_session(session_id)
        if not session or session.get("status") != "running":
            break
        
        has_acted, desc = MeetingEngine.step(session)
        if not has_acted:
            break
        
        time.sleep(0.5)

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/static/<path:path>")
def serve_static(path):
    return send_from_directory("static", path)

# ----------------- Scanner & Settings API -----------------
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
    save_settings(data)
    return jsonify({"success": True, "settings": data})

# ----------------- Sessions API -----------------
@app.route("/api/sessions", methods=["GET"])
def api_list_sessions():
    return jsonify(list_sessions())

@app.route("/api/sessions", methods=["POST"])
def api_create_session():
    data = request.json or {}
    title = data.get("title", "新架构评审会议").strip() or "新架构评审会议"
    employee_ids = data.get("employee_ids")
    moderator_id = data.get("moderator_id")
    workspace_dir = data.get("workspace_dir")
    
    session = create_session(
        title=title,
        employee_ids=employee_ids,
        moderator_id=moderator_id,
        workspace_dir=workspace_dir
    )
    return jsonify(session)

@app.route("/api/sessions/<session_id>", methods=["GET"])
def api_get_session(session_id):
    session = get_session(session_id)
    if not session:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify(session)

@app.route("/api/sessions/<session_id>", methods=["PUT"])
def api_update_session(session_id):
    session = get_session(session_id)
    if not session:
        return jsonify({"error": "会话不存在"}), 404
    
    data = request.json or {}
    if "title" in data:
        session["title"] = data["title"].strip() or session["title"]
    if "is_favorite" in data:
        session["is_favorite"] = bool(data["is_favorite"])
    if "employee_ids" in data:
        session["employee_ids"] = data["employee_ids"]
    if "moderator_id" in data:
        session["moderator_id"] = data["moderator_id"]
    if "workspace_dir" in data:
        session["workspace_dir"] = os.path.abspath(data["workspace_dir"])
    if "document" in data:
        session["document"] = data["document"]
        
    save_session(session)
    return jsonify(session)

@app.route("/api/sessions/<session_id>", methods=["DELETE"])
def api_delete_session(session_id):
    success = delete_session(session_id)
    return jsonify({"success": success})

# ----------------- Workspace Explorer API -----------------
@app.route("/api/workspace/files", methods=["GET"])
def api_workspace_files():
    """列出工作目录下的文件列表"""
    path = request.args.get("path", "").strip()
    if not path or not os.path.exists(path):
        return jsonify({"error": "路径不存在", "files": []})
    
    items = []
    try:
        for entry in os.scandir(path):
            # 过滤隐藏文件与巨型目录
            if entry.name.startswith(".") or entry.name in ["node_modules", ".git", "__pycache__", "venv"]:
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
    """读取指定文件内容"""
    fpath = request.args.get("path", "").strip()
    if not fpath or not os.path.exists(fpath) or not os.path.isfile(fpath):
        return jsonify({"error": "文件不存在"}), 404
    
    # 防止读取超大二进制文件 (限制最大 500KB)
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
    session = get_session(session_id)
    if not session:
        return jsonify({"error": "会话不存在"}), 404
    
    data = request.json or {}
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"error": "发言内容不能为空"}), 400
    
    MeetingEngine.user_inject(session, content)
    
    # 启动后台线程推进
    thread = threading.Thread(target=background_runner, args=(session_id,), daemon=True)
    thread.start()
    
    return jsonify(get_session(session_id))

@app.route("/api/sessions/<session_id>/pause", methods=["POST"])
def api_pause(session_id):
    """强行暂停会议并立即中断正在跑的 CLI 进程"""
    session = get_session(session_id)
    if not session:
        return jsonify({"error": "会话不存在"}), 404
    
    session["status"] = "paused"
    session["current_speaker"] = None
    save_session(session)

    # 毫秒级干掉正在运行的子进程树
    killed = Executor.kill_session_process(session_id)
    
    # 添加系统广播
    now = int(time.time())
    session["messages"].append({
        "id": f"msg_sys_{now}",
        "sender": "system",
        "sender_name": "系统广播",
        "content": "🛑 会议已被群主手动暂停，正在运行的 AI 任务已立即终止中断。",
        "phase": session.get("phase", 1),
        "verdict": "PAUSE",
        "elapsed_time": 0,
        "created_at": now
    })
    save_session(session)
    
    return jsonify({"success": True, "killed": killed, "status": "paused"})

@app.route("/api/sessions/<session_id>/resume", methods=["POST"])
def api_resume(session_id):
    """恢复会议继续运行"""
    session = get_session(session_id)
    if not session:
        return jsonify({"error": "会话不存在"}), 404
    
    session["status"] = "running"
    save_session(session)
    
    thread = threading.Thread(target=background_runner, args=(session_id,), daemon=True)
    thread.start()
    return jsonify({"success": True, "status": "running"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"\n🚀 AI-Meeting (AI 圆桌会议室) 启动成功!")
    print(f"👉 访问地址: http://127.0.0.1:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
