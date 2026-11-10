import os
import threading
import time
from flask import Flask, request, jsonify, send_from_directory, Response
from core.storage import (
    load_settings, save_settings,
    list_sessions, get_session, create_session, save_session, delete_session
)
from core.engine import MeetingEngine

app = Flask(__name__, static_folder="static")

# 后台自动推进线程字典: { session_id: threading.Thread }
RUNNING_THREADS = {}

def background_runner(session_id: str):
    """后台持续推进会议，直到暂停或结束"""
    while True:
        session = get_session(session_id)
        if not session or session.get("status") != "running":
            break
        
        has_acted, desc = MeetingEngine.step(session)
        if not has_acted:
            break
        
        # 步与步之间微缓冲 0.5 秒，方便前端观察
        time.sleep(0.5)

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/static/<path:path>")
def serve_static(path):
    return send_from_directory("static", path)

# ----------------- Settings API -----------------
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
    title = data.get("title", "新架构评审会议")
    employee_ids = data.get("employee_ids")
    session = create_session(title=title, employee_ids=employee_ids)
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
    if "document" in data:
        session["document"] = data["document"]
        
    save_session(session)
    return jsonify(session)

@app.route("/api/sessions/<session_id>", methods=["DELETE"])
def api_delete_session(session_id):
    success = delete_session(session_id)
    return jsonify({"success": success})

# ----------------- Meeting Controls API -----------------
@app.route("/api/sessions/<session_id>/message", methods=["POST"])
def api_send_message(session_id):
    session = get_session(session_id)
    if not session:
        return jsonify({"error": "会话不存在"}), 404
    
    data = request.json or {}
    content = data.get("content", "").strip()
    is_new_direction = data.get("is_new_direction", False)
    
    if not content:
        return jsonify({"error": "发言内容不能为空"}), 400
    
    MeetingEngine.user_inject(session, content, is_new_direction=is_new_direction)
    
    # 用户发言后，自动启动后台运行推进
    thread = threading.Thread(target=background_runner, args=(session_id,), daemon=True)
    thread.start()
    
    return jsonify(get_session(session_id))

@app.route("/api/sessions/<session_id>/step", methods=["POST"])
def api_step(session_id):
    """单步推进一次"""
    session = get_session(session_id)
    if not session:
        return jsonify({"error": "会话不存在"}), 404
    
    if session.get("status") == "paused":
        session["status"] = "running"
        save_session(session)

    has_acted, desc = MeetingEngine.step(session)
    updated_session = get_session(session_id)
    return jsonify({
        "acted": has_acted,
        "message": desc,
        "session": updated_session
    })

@app.route("/api/sessions/<session_id>/auto", methods=["POST"])
def api_auto_run(session_id):
    """启动自动讨论推进"""
    session = get_session(session_id)
    if not session:
        return jsonify({"error": "会话不存在"}), 404
    
    session["status"] = "running"
    save_session(session)
    
    thread = threading.Thread(target=background_runner, args=(session_id,), daemon=True)
    thread.start()
    return jsonify({"success": True, "status": "running"})

@app.route("/api/sessions/<session_id>/pause", methods=["POST"])
def api_pause(session_id):
    """暂停会议"""
    session = get_session(session_id)
    if not session:
        return jsonify({"error": "会话不存在"}), 404
    
    session["status"] = "paused"
    save_session(session)
    return jsonify({"success": True, "status": "paused"})

@app.route("/api/sessions/<session_id>/resume", methods=["POST"])
def api_resume(session_id):
    """恢复会议并继续自动推进"""
    session = get_session(session_id)
    if not session:
        return jsonify({"error": "会话不存在"}), 404
    
    session["status"] = "running"
    save_session(session)
    
    thread = threading.Thread(target=background_runner, args=(session_id,), daemon=True)
    thread.start()
    return jsonify({"success": True, "status": "running"})

@app.route("/api/sessions/<session_id>/export", methods=["GET"])
def api_export_doc(session_id):
    """导出 Markdown 文档"""
    session = get_session(session_id)
    if not session:
        return "会话不存在", 404
    
    doc_content = session.get("document", "# 方案草案")
    title = session.get("title", "方案")
    filename = f"{title}.md".replace("/", "_").replace(" ", "_")
    
    return Response(
        doc_content,
        mimetype="text/markdown",
        headers={"Content-disposition": f"attachment; filename={filename}"}
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"\n🚀 AI-Meeting (AI 圆桌会议室) 启动成功!")
    print(f"👉 访问地址: http://127.0.0.1:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
