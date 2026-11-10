import os
import threading
import time
import re
from typing import List, Optional
from flask import Flask, request, jsonify, send_from_directory

from core.storage import (
    load_settings, save_settings,
    list_sessions, get_session, create_session, save_session, delete_session,
    is_valid_session_id, session_lock, SessionDeletedError
)
from core.engine import MeetingEngine, generate_msg_id, bump_document_revision
from core.executor import Executor, validate_cli_template
from core.scanner import scan_local_models

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(__name__, static_folder=STATIC_DIR)

# 仅接受本机 Host 访问：抵御 DNS rebinding（恶意网页把自有域名解析到 127.0.0.1 后
# 即可绕过同源策略读取本地文件/改写配置）
_ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}


@app.before_request
def _guard_host():
    host = (request.host or "").strip().lower()
    if host.startswith("["):
        hostname = host.split("]")[0].strip("[")
    else:
        hostname = host.rsplit(":", 1)[0]
    if hostname and hostname not in _ALLOWED_HOSTNAMES:
        return jsonify({"error": "forbidden host"}), 403
    return None


def _json_body() -> dict:
    """安全读取 JSON 请求体：非 JSON / 空 body 一律返回 {}，不抛 415。"""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _clean_employee_ids(raw) -> Optional[List[str]]:
    """校验并规范化参会成员 id 列表（只保留字符串、去重、剔除未知角色）。"""
    if not isinstance(raw, list):
        return None
    if any(not isinstance(item, str) for item in raw):
        return None
    settings = load_settings()
    known = {e.get("id") for e in settings.get("employees", []) if isinstance(e, dict)}
    result: List[str] = []
    for item in raw:
        if item in known and item not in result:
            result.append(item)
    return result


# ----------------- 并发线程管理器 -----------------
_RUNNER_LOCK = threading.Lock()
_ACTIVE_SESSIONS = set()
# 记录「线程还在跑时用户点了恢复」的会话，线程退出后据此重排一次，避免恢复被吞掉
_PENDING_RESUME = set()

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
        except SessionDeletedError:
            print(f"[MeetingRunner] 会话 {session_id} 已被删除，停止推进")
        except Exception as ex:
            print(f"[MeetingRunner] 异常捕获 (Session: {session_id}):", ex)
            try:
                with session_lock(session_id):
                    curr_s = get_session(session_id)
                    if curr_s:
                        curr_s["status"] = "paused"
                        curr_s["current_speaker"] = None
                        curr_s.setdefault("messages", []).append({
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
                pending = session_id in _PENDING_RESUME
                _PENDING_RESUME.discard(session_id)

            # 用户在线程运行期间点过「恢复」：这里补排一次，否则恢复会被本次退出吞掉
            if pending:
                try:
                    latest = get_session(session_id)
                    if latest and latest.get("status") == "running":
                        start_background_runner(session_id)
                except Exception as e_restart:
                    print("[MeetingRunner] 补排恢复失败:", e_restart)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


def request_resume(session_id: str):
    """恢复会议：若已有线程在跑，登记待续标记，由退出时的 finally 补排。"""
    with _RUNNER_LOCK:
        if session_id in _ACTIVE_SESSIONS:
            _PENDING_RESUME.add(session_id)
            return
    start_background_runner(session_id)

# ----------------- 路径安全过滤助手 -----------------
# 安全模型：只允许「家目录 / 当前工作区」内的路径，且路径中任何一段都不允许是隐藏项
# （.ssh .gnupg .aws .kube .config .workbuddy .gitconfig .docker .zprofile .netrc …），
# 再加少量绝对目录黑名单。黑名单天然漏，所以以「隐藏段一律拒绝」为主规则。
SENSITIVE_PATTERNS = [
    re.compile(r"(^|[/\\])Library([/\\]|$)"),
    re.compile(r"(^|[/\\])\.Trash([/\\]|$)"),
    re.compile(r"(^|[/\\])(id_rsa|id_ed25519|id_ecdsa|credentials|\.netrc|\.git-credentials)([/\\]|$)", re.I),
]

BLOCKED_ROOTS = ("/etc", "/private", "/proc", "/sys", "/System", "/Library", "/usr", "/bin", "/sbin", "/var", "/dev")


def _has_hidden_component(real_path: str, base: str) -> bool:
    """判断路径相对 base 的任一段是否以 '.' 开头（即点文件/点目录）。"""
    try:
        rel = os.path.relpath(real_path, base)
    except ValueError:
        return True
    if rel in (".", ""):
        return False
    return any(part.startswith(".") for part in rel.split(os.sep) if part not in ("", "."))


def is_path_safe(target_path: str) -> bool:
    """严格校验路径安全性，杜绝路径穿越、系统文件泄露与敏感凭据窃取"""
    if not target_path or not isinstance(target_path, str):
        return False

    raw = target_path.strip()
    if not raw or "\x00" in raw:
        return False

    real_path = os.path.realpath(os.path.expanduser(raw))
    user_home = os.path.realpath(os.path.expanduser("~"))
    cwd = os.path.realpath(os.getcwd())

    if real_path == user_home or real_path.startswith(user_home + os.sep):
        base = user_home
    elif real_path == cwd or real_path.startswith(cwd + os.sep):
        base = cwd
    else:
        return False

    for bad in BLOCKED_ROOTS:
        if real_path == bad or real_path.startswith(bad + os.sep):
            return False

    if _has_hidden_component(real_path, base):
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

# 只在真正运行服务的进程里执行一次自愈清理：
# debug 模式下 reloader 会启动父子两个进程，父进程不该动数据（否则热重载会重复写盘）
_DEBUG_ENV = os.environ.get("FLASK_DEBUG", "1") in ["1", "true"]
if (not _DEBUG_ENV) or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    sanitize_sessions_on_startup()

# ----------------- 静态页面路由 -----------------
@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")

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

AGENT_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _sanitize_agent(raw, label: str, default_id: str) -> "tuple":
    """把前端提交的角色配置规范化成安全结构，非法项直接拒绝。"""
    if not isinstance(raw, dict):
        return None, f"{label} 配置必须为对象"

    cli_cmd = raw.get("cli_cmd")
    if not isinstance(cli_cmd, str) or not cli_cmd.strip():
        return None, f"{label} 缺少 cli_cmd"
    cli_cmd = cli_cmd.strip()

    ok, err = validate_cli_template(cli_cmd)
    if not ok:
        return None, f"{label} CLI 命令不合法: {err}"

    agent_id = str(raw.get("id") or "").strip() or default_id
    if not AGENT_ID_REGEX.match(agent_id):
        return None, f"{label} 的 id 不合法（仅允许字母、数字、下划线、短横线）"

    try:
        timeout = int(float(raw.get("timeout", 180)))
    except (TypeError, ValueError):
        timeout = 180
    timeout = max(10, min(3600, timeout))

    return {
        "id": agent_id,
        "name": str(raw.get("name") or "").strip() or label,
        "avatar": str(raw.get("avatar") or "🤖")[:8],
        "avatar_color": str(raw.get("avatar_color") or "#3b82f6")[:32],
        "type": raw.get("type") if raw.get("type") in ("cli", "api") else "cli",
        "cli_cmd": cli_cmd,
        "system_prompt": str(raw.get("system_prompt") or "")[:8000],
        "enabled": bool(raw.get("enabled", True)),
        "timeout": timeout,
    }, None


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    data = _json_body()

    # 严格校验配置 Schema 与 CLI 命令合法性
    moderator, err = _sanitize_agent(data.get("moderator"), "主持人", "moderator")
    if err:
        return jsonify({"error": err}), 400

    employees_raw = data.get("employees", [])
    if not isinstance(employees_raw, list):
        return jsonify({"error": "employees 必须为列表"}), 400

    employees = []
    seen_ids = {moderator["id"]}
    for idx, emp in enumerate(employees_raw):
        cleaned, err = _sanitize_agent(emp, f"第 {idx + 1} 个员工", f"emp_{idx + 1}")
        if err:
            return jsonify({"error": err}), 400
        if cleaned["id"] in seen_ids:
            return jsonify({"error": f"角色 id 重复: {cleaned['id']}"}), 400
        seen_ids.add(cleaned["id"])
        employees.append(cleaned)

    payload = dict(data)
    payload["moderator"] = moderator
    payload["employees"] = employees

    save_settings(payload)
    return jsonify({"success": True, "settings": payload})

# ----------------- Sessions API -----------------
@app.route("/api/sessions", methods=["GET"])
def api_list_sessions():
    return jsonify(list_sessions())

@app.route("/api/sessions", methods=["POST"])
def api_create_session():
    data = _json_body()
    title = str(data.get("title") or "").strip() or "新方案研讨会议"
    title = title[:200]

    # 传 None 时由 storage 使用「默认启用名单」
    employee_ids = _clean_employee_ids(data.get("employee_ids"))

    workspace_dir = data.get("workspace_dir")
    if not isinstance(workspace_dir, str) or not is_path_safe(workspace_dir) or not os.path.isdir(os.path.expanduser(workspace_dir)):
        workspace_dir = os.getcwd()

    session = create_session(
        title=title,
        employee_ids=employee_ids,
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

    data = _json_body()

    with session_lock(session_id):
        session = get_session(session_id)
        if not session:
            return jsonify({"error": "会话不存在"}), 404

        if "title" in data:
            session["title"] = str(data["title"]).strip()[:200] or session["title"]
        if "is_favorite" in data:
            session["is_favorite"] = bool(data["is_favorite"])

        if "employee_ids" in data:
            new_ids = _clean_employee_ids(data.get("employee_ids"))
            if new_ids is None:
                return jsonify({"error": "employee_ids 必须为字符串数组"}), 400
            old_ids = [x for x in (session.get("employee_ids") or []) if isinstance(x, str)]
            if set(old_ids) != set(new_ids):
                added_ids = [x for x in new_ids if x not in old_ids]
                removed_ids = [x for x in old_ids if x not in new_ids]
                settings = load_settings()
                parts = []
                if added_ids:
                    added_names = [MeetingEngine.get_employee(settings, eid).get("name", eid) for eid in added_ids]
                    parts.append(f"加入了 [{', '.join(added_names)}]")
                if removed_ids:
                    removed_names = [MeetingEngine.get_employee(settings, eid).get("name", eid) for eid in removed_ids]
                    parts.append(f"移出了 [{', '.join(removed_names)}]")

                notice_text = f"📢 参会人员变动：{'；'.join(parts)}"
                session.setdefault("messages", []).append({
                    "id": generate_msg_id(),
                    "sender": "system",
                    "sender_name": "系统广播",
                    "content": notice_text,
                    "phase": session.get("phase", 1),
                    "verdict": "MEMBER_CHANGE",
                    "elapsed_time": 0,
                    "created_at": int(time.time()),
                    "sent_prompt": "",
                    "raw_response": ""
                })
                session["employee_ids"] = new_ids

        if "workspace_dir" in data:
            raw_dir = str(data.get("workspace_dir") or "").strip()
            expanded = os.path.expanduser(raw_dir) if raw_dir else ""
            if expanded and is_path_safe(expanded) and os.path.isdir(expanded):
                session["workspace_dir"] = os.path.abspath(expanded)
            else:
                return jsonify({"error": "工作目录非法或不是有效目录"}), 400

        if "document" in data:
            document = data.get("document")
            if not isinstance(document, str):
                return jsonify({"error": "document 必须为字符串"}), 400
            if len(document) > 2_000_000:
                return jsonify({"error": "document 过大（上限 2MB）"}), 400
            # 人工改写正文同样开新一轮：上一版文档上的审阅意见不再带入后续 prompt，
            # 已 PASS 的结论也一并作废（针对旧正文的通过不能代表新正文）
            if document != session.get("document"):
                session["document"] = document
                bump_document_revision(session)
                session["passed_employees"] = []
                session.setdefault("messages", []).append({
                    "id": generate_msg_id(),
                    "sender": "system",
                    "sender_name": "系统广播",
                    "content": "📝 群主改写了方案正文，审阅轮次已重置，所有成员将基于新版重新审阅。",
                    "phase": session.get("phase", 1),
                    "verdict": "DOC_EDIT",
                    "elapsed_time": 0,
                    "created_at": int(time.time()),
                    "sent_prompt": "",
                    "raw_response": ""
                })

        save_session(session)
        return jsonify(session)

@app.route("/api/sessions/<session_id>", methods=["DELETE"])
def api_delete_session(session_id):
    if not is_valid_session_id(session_id):
        return jsonify({"error": "非法 session_id"}), 400

    Executor.kill_session_process(session_id)
    success = delete_session(session_id)
    with _RUNNER_LOCK:
        _PENDING_RESUME.discard(session_id)
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
            try:
                is_dir = entry.is_dir()
                size = entry.stat().st_size if entry.is_file() else 0
            except OSError:
                # 断链的软链接 / 无权限项直接跳过，不能让整个列表接口 500
                continue
            items.append({
                "name": entry.name,
                "path": entry.path,
                "is_dir": is_dir,
                "size": size
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

    data = _json_body()
    content = str(data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "发言内容不能为空"}), 400
    if len(content) > 20000:
        return jsonify({"error": "发言内容过长（上限 20000 字符）"}), 400

    with session_lock(session_id):
        session = get_session(session_id)
        if not session:
            return jsonify({"error": "会话不存在"}), 404
        MeetingEngine.user_inject(session, content)

    start_background_runner(session_id)

    return jsonify(get_session(session_id) or {})

@app.route("/api/sessions/<session_id>/pause", methods=["POST"])
def api_pause(session_id):
    if not is_valid_session_id(session_id):
        return jsonify({"error": "非法 session_id"}), 400

    # 状态与广播消息在同一次加锁写入里完成，避免后台线程用旧副本回写覆盖
    with session_lock(session_id):
        session = get_session(session_id)
        if not session:
            return jsonify({"error": "会话不存在"}), 404

        session["status"] = "paused"
        session["current_speaker"] = None
        session.setdefault("messages", []).append({
            "id": generate_msg_id(),
            "sender": "system",
            "sender_name": "系统广播",
            "content": "🛑 会议已被群主手动暂停，正在运行的 AI 任务已立即终止中断。",
            "phase": session.get("phase", 1),
            "verdict": "PAUSE",
            "elapsed_time": 0,
            "created_at": int(time.time()),
            "sent_prompt": "",
            "raw_response": ""
        })
        save_session(session)

    # 进程终止放在锁外，避免阻塞其他会话操作
    killed = Executor.kill_session_process(session_id)

    return jsonify({"success": True, "killed": killed, "status": "paused"})

@app.route("/api/sessions/<session_id>/resume", methods=["POST"])
def api_resume(session_id):
    if not is_valid_session_id(session_id):
        return jsonify({"error": "非法 session_id"}), 400

    with session_lock(session_id):
        session = get_session(session_id)
        if not session:
            return jsonify({"error": "会话不存在"}), 404

        session["status"] = "running"
        session["current_speaker"] = None
        save_session(session)

    request_resume(session_id)
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
