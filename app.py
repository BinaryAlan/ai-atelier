import os
import sys
import subprocess
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
from core.engine import (
    MeetingEngine, generate_msg_id, bump_document_revision, run_with_retry,
    request_skip_current, clear_skip_current,
    build_direct_prompt, latest_user_message, find_quote_by_question,
)
from core.executor import Executor, validate_cli_template
from core.scanner import scan_local_models

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# 启动时刻与运行模式（供 /api/system/status 展示，也让「重启/停止」知道该走哪条路）
_STARTED_AT = time.time()
_DEBUG_ENV = os.environ.get("FLASK_DEBUG", "1") in ["1", "true"]
# werkzeug reloader 会用它自己启动子进程并置位该变量；据此判断当前是「reloader 子进程」
_UNDER_RELOADER = os.environ.get("WERKZEUG_RUN_MAIN") == "true"

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
                speaker = (full_s.get("current_speaker") or "").strip()
                if full_s.get("current_speaker"):
                    full_s["current_speaker"] = None
                    changed = True
                if full_s.get("status") == "running":
                    full_s["status"] = "paused"
                    where = f"（当时停在【{speaker}】发言中途）" if speaker else ""
                    full_s["messages"].append({
                        "id": generate_msg_id(),
                        "sender": "system",
                        "sender_name": "系统复位",
                        "content": (
                            f"⚠️ 服务端已重启，后台进程被终止，会议转为暂停{where}。\n"
                            "点右上角「▶️ 恢复讨论」即可继续（也可以直接点这条消息下方的「继续会议」）。\n"
                            "如果连续两次都停在同一人身上，系统会自动暂缓它、让余下成员先走。"
                        ),
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
if (not _DEBUG_ENV) or _UNDER_RELOADER:
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
        timeout = int(float(raw.get("timeout", 0)))
    except (TypeError, ValueError):
        timeout = 0
    # 0 = 不限制（默认）：模型可以跑到自己结束，需要打断时用「⏭ 跳过当前发言」
    timeout = max(0, min(86400, timeout))

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
                # 被移出的人不该继续挂在「跳过名单」里，否则重新拉回来时要多点一次恢复
                session["skipped_employees"] = [
                    e for e in (session.get("skipped_employees") or []) if e in new_ids
                ]

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
                session["blocker_under_review"] = None
                session["blocker_retry_count"] = 0
                session["review_comment_retries"] = {}
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

    # 「💬 回复」某条发言：前端只传被引用消息的 id，引用对象与定向目标一律以服务端
    # 存档为准（不信任客户端传来的发言人/正文），避免伪造引用去骗某个模型。
    reply_to = data.get("reply_to")
    reply_id = ""
    if isinstance(reply_to, dict):
        reply_id = str(reply_to.get("id") or "").strip()
    elif isinstance(reply_to, str):
        reply_id = reply_to.strip()

    with session_lock(session_id):
        session = get_session(session_id)
        if not session:
            return jsonify({"error": "会话不存在"}), 404

        if reply_id:
            quoted = _find_message(session, reply_id)
            if not quoted:
                return jsonify({"error": "被回复的消息不存在，可能已被删除"}), 404
            sender = quoted.get("sender")
            if sender in ("user", "system"):
                return jsonify({"error": "只能回复成员或主持人的发言"}), 400
            allowed = set(session.get("employee_ids") or [])
            allowed.add(MeetingEngine.get_moderator(load_settings()).get("id"))
            if sender not in allowed:
                return jsonify({"error": "该成员已不在本场会议中，无法直接回复"}), 400

        MeetingEngine.user_inject(session, content, reply_to=reply_id or None)

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


@app.route("/api/sessions/<session_id>/interrupt", methods=["POST"])
def api_interrupt_current(session_id):
    """
    中断当前正在作答的成员，并把它移出发言位，让余下成员接着走。

    和「暂停」的区别：暂停会把整场会议停下来等人；这里只换人，会议继续跑。
    实现上先登记跳过请求再杀进程——引擎线程拿到 interrupted 结果时靠这个登记
    判断「这是要跳过，不是要暂停」，否则会被当成暂停处理。
    """
    if not is_valid_session_id(session_id):
        return jsonify({"error": "非法 session_id"}), 400
    if not _require_action_header():
        return jsonify({"error": "缺少操作确认头"}), 400

    speaker = None
    with session_lock(session_id):
        session = get_session(session_id)
        if not session:
            return jsonify({"error": "会话不存在"}), 404
        if session.get("status") != "running":
            return jsonify({"error": "会议当前不在推进中，没有可以中断的发言"}), 409
        speaker = session.get("current_speaker")
        if not speaker:
            # 状态是 running 但还没点名（线程刚起步）：稍等一下再点
            return jsonify({"error": "还没开始下一位发言，请稍后再点"}), 409

    request_skip_current(session_id)
    killed = Executor.kill_session_process(session_id)
    if not killed:
        # 没杀到进程：撤销登记，避免这条请求串到下一次发言上
        clear_skip_current(session_id)
        return jsonify({
            "error": "没找到正在运行的进程（可能刚好答完了）。刷新一下页面再试，"
                     "若确实还在跑，请用「⏸️ 暂停会议」"
        }), 409

    return jsonify({"success": True, "interrupted": speaker, "session": get_session(session_id) or {}}), 202


@app.route("/api/sessions/<session_id>/resume", methods=["POST"])
def api_resume(session_id):
    if not is_valid_session_id(session_id):
        return jsonify({"error": "非法 session_id"}), 400

    auto_skipped_name = None
    stuck_reason = None
    retried_same = False

    with session_lock(session_id):
        session = get_session(session_id)
        if not session:
            return jsonify({"error": "会话不存在"}), 404

        settings = load_settings()
        workers = MeetingEngine.active_workers(session, settings)
        if not workers:
            return jsonify({
                "error": "本场已经没有能发言的成员（都被跳过，或未配置可用的 CLI）。"
                         "请先到「👥 参会成员」面板恢复成员，再点「恢复讨论」。"
            }), 409

        # ---- 「恢复」不等于「原地再撞一次墙」----
        # 上次是因为某位成员卡住才暂停的，就记着它：
        #   第一次恢复 -> 给它一次新的机会（同时把这个判断链上的计数清零）；
        #   仍停在同一人 -> 自动暂缓它，让余下成员继续，否则点多少次都一样。
        stuck = session.get("stuck") or {}
        stuck_id = stuck.get("employee_id")
        stuck_reason = stuck.get("reason") or "未给出明确结论"
        attempts = int(session.get("stuck_resume_attempts") or 0)

        if not stuck_id:
            # 旧版本数据没有 stuck 字段，但失败次数还留在 review_comment_retries 里：
            # 已经用光两次机会的人不该因为缺一个字段就再撞一遍。
            exhausted = [
                eid for eid, n in (session.get("review_comment_retries") or {}).items()
                if isinstance(n, int) and n >= 2 and eid in workers
            ]
            if exhausted:
                stuck_id = exhausted[0]
                stuck_reason = "已连续 2 次未给出明确结论"
                attempts = 1    # 视为「已经给过机会」，本次恢复直接放行余下成员

        if stuck_id and stuck_id in workers:
            # 已经为这个卡点恢复过一次、结果还是停在同一个人 -> 不再让它挡路
            if attempts >= 1:
                if len(workers) <= 1:
                    return jsonify({
                        "error": f"能发言的只剩 [{MeetingEngine.get_employee(settings, stuck_id).get('name')}] 一个，"
                                 f"它上一次没给出结论（{stuck_reason}）。再暂缓就没人可审了："
                                 "请先给它换一个可用的模型/CLI，或直接发消息给出新指令。"
                    }), 409
                skipped = list(session.get("skipped_employees") or [])
                if stuck_id not in skipped:
                    skipped.append(stuck_id)
                session["skipped_employees"] = skipped
                emp = MeetingEngine.get_employee(settings, stuck_id)
                auto_skipped_name = emp.get("name") or stuck_id
                session.pop("stuck", None)
                session.pop("stuck_resume_attempts", None)
                _append_system_message(
                    session,
                    f"⏭ [{auto_skipped_name}] 连续未能给出结论（{stuck_reason}）。"
                    "本次恢复不再重复问它，自动把它移出发言位，会议改由余下成员继续。"
                    "需要它的意见时，到「👥 参会成员」面板点「↩️ 恢复」即可把它拉回来。",
                )
            else:
                session["stuck_resume_attempts"] = 1
                retried_same = True
        else:
            # 上次不是卡在某个人身上（例如群主手动暂停）：本次恢复同样算「给了它一次机会」
            session["stuck_resume_attempts"] = 1

        # 收敛计数必须清零。不清的话，恢复后引擎会拿上一轮的旧计数立刻再次判定
        # 「连续 2 次无结论」并暂停 —— 用户看到的就是「点了恢复，几秒后又暂停」。
        session["review_comment_retries"] = {}
        session["blocker_retry_count"] = 0

        session["status"] = "running"
        session["current_speaker"] = None
        save_session(session)

    request_resume(session_id)

    if auto_skipped_name:
        message = f"已自动暂缓 {auto_skipped_name}，余下成员继续。"
    elif retried_same:
        message = f"已让上一次停下的成员重新表态（上次原因：{stuck_reason}）。若仍停在同一人，再点一次会自动暂缓它。"
    else:
        message = "会议已继续。"

    return jsonify({
        "success": True,
        "status": "running",
        "auto_skipped": auto_skipped_name,
        "message": message,
    })


# ----------------- 单个成员的异常恢复（重试 / 跳过） -----------------
# 某个 CLI 偶发超时/报错时，过去只能「整场重开」——这对一场已经聊了半天的会议代价太高。
# 这里提供两件更细的工具：
#   1) 单独重试那一条发言：原地覆盖结果，不产生重复消息，也不重跑其他人；
#   2) 跳过该成员：它不再占用发言位，余下成员照常推进（配置与历史发言都保留，可随时恢复）。
_RETRY_LOCK = threading.Lock()
_RETRY_INFLIGHT: set = set()


def _find_message(session: dict, msg_id: str) -> Optional[dict]:
    for m in session.get("messages") or []:
        if isinstance(m, dict) and m.get("id") == msg_id:
            return m
    return None


def _direct_question(session: dict, msg: dict) -> str:
    """
    取回「群主当时到底问了什么」，用于重试时重建 prompt。

    优先用记录字段；旧数据从当时的 prompt 模板里反解析；再退化为
    「最近一条 @ 了这位成员的用户消息」——绝不能直接拿 latest_user_message，
    因为群主可能在失败之后又发了别的话，那会问错问题。
    """
    q = str(msg.get("direct_question") or "").strip()
    if q:
        return q

    old_prompt = str(msg.get("sent_prompt") or "")
    m = re.search(r"【会议群主直接提问/指令】\s*[:：]\s*(.+)", old_prompt)
    if m:
        return m.group(1).strip()

    emp_name = str(msg.get("sender_name") or "").strip()
    for item in reversed(session.get("messages") or []):
        if not isinstance(item, dict) or item.get("sender") != "user":
            continue
        content = str(item.get("content") or "").strip()
        if content and emp_name and f"@{emp_name}" in content:
            return content
    return latest_user_message(session).strip()


def _append_system_message(session: dict, text: str, verdict: str = "RECOVER") -> None:
    session.setdefault("messages", []).append({
        "id": generate_msg_id(),
        "sender": "system",
        "sender_name": "系统广播",
        "content": text,
        "phase": session.get("phase", 1),
        "verdict": verdict,
        "elapsed_time": 0,
        "created_at": int(time.time()),
        "sent_prompt": "",
        "raw_response": "",
    })


def _resolve_turn_employee(msg: dict, require_cli: bool = True) -> Optional[dict]:
    """取出发言人配置；主持人 / 系统消息 / 已删除角色不允许被单独重试或跳过。"""
    settings = load_settings()
    moderator = MeetingEngine.get_moderator(settings)
    emp_id = msg.get("sender")
    if not isinstance(emp_id, str) or not emp_id or emp_id in ("user", "system"):
        return None
    if emp_id == moderator.get("id"):
        return None

    emp = MeetingEngine.get_employee(settings, emp_id)
    if emp.get("id") == moderator.get("id"):
        return None
    if require_cli and (not emp.get("cli_cmd") or not emp.get("enabled", True)):
        return None
    return emp


@app.route("/api/sessions/<session_id>/messages/<msg_id>/retry", methods=["POST"])
def api_retry_message(session_id, msg_id):
    if not is_valid_session_id(session_id):
        return jsonify({"error": "非法 session_id"}), 400
    if not _require_action_header():
        return jsonify({"error": "缺少操作确认头"}), 400

    with _RETRY_LOCK:
        if msg_id in _RETRY_INFLIGHT:
            return jsonify({"error": "这条发言正在重试中，请稍候"}), 409
        with session_lock(session_id):
            session = get_session(session_id)
            if not session:
                return jsonify({"error": "会话不存在"}), 404
            if session_id in _ACTIVE_SESSIONS:
                return jsonify({"error": "会议正在推进中（有人正在发言），请等本轮结束再重试"}), 409
            msg = _find_message(session, msg_id)
            if not msg:
                return jsonify({"error": "找不到这条发言"}), 404
            if msg.get("verdict") not in ("ERROR", "TIMEOUT"):
                return jsonify({"error": "只有报错/超时的发言才需要重试"}), 400
            # 被 @ 的定向应答失败时只能同一个人重问（换人没意义），
            # 它不属于任何阶段的轮次，所以不受「阶段一才可重试」的限制。
            is_direct = bool(msg.get("direct_mention"))
            if not is_direct:
                if msg.get("phase") != 1:
                    return jsonify({
                        "error": "阶段二的异常可以直接「恢复讨论」重问同一位成员；"
                                 "若不想再等它，点「跳过此人」即可"
                    }), 400
                if session.get("phase", 1) != 1:
                    return jsonify({"error": "会议已进入阶段二，补一条阶段一的立论已经没有意义"}), 400
            emp = _resolve_turn_employee(msg)
            if not emp:
                return jsonify({"error": "该角色已删除或未配置 CLI，无法重试"}), 400

            workspace_dir = session.get("workspace_dir", "")
            if is_direct:
                # 早前的定向应答 prompt 只有「问题 + 人设 + 工作目录」，
                # 没带聊天上下文与方案纪要，模型等于在真空里作答。
                # 重试时按当前会话状态重建，并把重建后的完整 prompt 写回记录，
                # 群主点开「查看 Prompt」看到的就是真正发出去的那一份。
                question = _direct_question(session, msg)
                quoted = msg.get("quoted") if isinstance(msg.get("quoted"), dict) else None
                if not quoted:
                    # 旧记录没存引用快照：按提问原文反查群主当时回复的是哪条
                    quoted = find_quote_by_question(session, question)
                prompt = build_direct_prompt(
                    session, emp, question,
                    workspace_dir=workspace_dir,
                    exclude_msg_id=msg_id,
                    is_retry=True,
                    quoted=quoted,
                )
                msg["sent_prompt"] = prompt
                msg["prompt_rebuilt_at"] = int(time.time())
            else:
                prompt = str(msg.get("sent_prompt") or "").strip()
            if not prompt:
                return jsonify({"error": "这条记录没有留存 Prompt（早于该功能上线），无法原样重试"}), 400
            emp_name = emp.get("name") or emp.get("id")
            prev_status = session.get("status", "idle")
            session["current_speaker"] = f"{emp_name} (重新发言中)"
            save_session(session)

        _RETRY_INFLIGHT.add(msg_id)

    def _worker():
        resume = False
        try:
            res = run_with_retry(emp, prompt, session_id, workspace_dir)
            with session_lock(session_id):
                curr = get_session(session_id)
                if not curr:
                    return
                target = _find_message(curr, msg_id)
                if not target:
                    return
                target["content"] = res.output
                target["raw_response"] = res.raw_output
                target["elapsed_time"] = res.elapsed
                target["retry_count"] = int(target.get("retry_count") or 0) + 1
                target["retried_at"] = int(time.time())
                curr["current_speaker"] = None

                if res.status == "success":
                    target["verdict"] = "DIRECT" if is_direct else "OPINION"
                    if is_direct:
                        # 只是一次点名问答，不该把整场会议重新推起来
                        curr["status"] = prev_status if prev_status in ("running", "paused") else "idle"
                        resume = curr["status"] == "running"
                        _append_system_message(
                            curr,
                            f"🔄 已重新应答 [{emp_name}]，新结果已替换这一条。会议状态保持原样。"
                        )
                    else:
                        curr["status"] = "running"
                        _append_system_message(
                            curr,
                            f"🔄 已单独重试 [{emp_name}]，这一条发言用新结果替换（其他人不受影响）。会议继续。"
                        )
                        resume = True
                elif res.status in ("stopped", "interrupted"):
                    target["verdict"] = "TIMEOUT" if msg.get("verdict") == "TIMEOUT" else "ERROR"
                    curr["status"] = (prev_status if prev_status in ("running", "paused") else "idle") \
                        if is_direct else "paused"
                    _append_system_message(curr, f"⏸️ 单独重试 [{emp_name}] 已被用户手动暂停中断。")
                else:
                    target["verdict"] = "TIMEOUT" if res.status == "timeout" else "ERROR"
                    curr["status"] = (prev_status if prev_status in ("running", "paused") else "idle") \
                        if is_direct else "paused"
                    if is_direct:
                        _append_system_message(
                            curr,
                            f"⚠️ [{emp_name}] 这次点名应答仍然失败（{res.status}）。"
                            "它不是会议流程的一部分，会议状态没有改变；"
                            "可以再点一次「🔄 重试」，或先修好它的 CLI 参数。"
                        )
                    else:
                        _append_system_message(
                            curr,
                            f"⚠️ [{emp_name}] 重试仍然失败（{res.status}）。"
                            "可以点该条发言旁的「跳过此人」继续推进，或先修好它的 CLI"
                            "（例如 --tools 参数）再重试。"
                        )
                save_session(curr)
        except Exception as ex:
            print("[Retry] 单独重试失败:", ex)
            try:
                with session_lock(session_id):
                    curr = get_session(session_id)
                    if curr:
                        curr["current_speaker"] = None
                        curr["status"] = "paused"
                        _append_system_message(curr, f"⚠️ 重试过程出现异常: {ex}")
                        save_session(curr)
            except Exception as e2:
                print("[Retry] 异常状态落盘失败:", e2)
        finally:
            with _RETRY_LOCK:
                _RETRY_INFLIGHT.discard(msg_id)
        if resume:
            request_resume(session_id)

    threading.Thread(target=_worker, daemon=True, name=f"retry-{msg_id[:12]}").start()
    return jsonify({
        "success": True,
        "retrying": True,
        "session": get_session(session_id) or {}
    }), 202


@app.route("/api/sessions/<session_id>/messages/<msg_id>/skip", methods=["POST"])
def api_skip_message(session_id, msg_id):
    """跳过一位成员：它不再占用发言位，会议继续推进（可随时恢复）。"""
    if not is_valid_session_id(session_id):
        return jsonify({"error": "非法 session_id"}), 400
    if not _require_action_header():
        return jsonify({"error": "缺少操作确认头"}), 400

    with session_lock(session_id):
        session = get_session(session_id)
        if not session:
            return jsonify({"error": "会话不存在"}), 404
        if session_id in _ACTIVE_SESSIONS:
            return jsonify({"error": "会议正在推进中，请等本次发言结束再操作"}), 409

        msg = _find_message(session, msg_id)
        if not msg:
            return jsonify({"error": "找不到这条发言"}), 404
        if msg.get("verdict") not in ("ERROR", "TIMEOUT"):
            return jsonify({"error": "只有报错/超时的发言才能跳过"}), 400
        if msg.get("direct_mention"):
            # 群主点名要的答案：跳过这个人等于让这次提问没有下文，应该重试
            return jsonify({"error": "这是群主 @ 指定成员的应答，不能跳过；请点「🔄 重试」重新问它"}), 400

        emp = _resolve_turn_employee(msg, require_cli=False)
        if not emp:
            return jsonify({"error": "主持人或系统记录不能被跳过"}), 400

        emp_id, emp_name = emp.get("id"), emp.get("name") or emp.get("id")
        skipped = [e for e in (session.get("skipped_employees") or []) if isinstance(e, str)]
        if emp_id not in skipped:
            skipped.append(emp_id)
        session["skipped_employees"] = skipped

        # 若当前正在验收该成员提出的 BLOCKER，清理阻断状态让其他成员继续
        blocker = session.get("blocker_under_review")
        if blocker and blocker.get("raised_by") == emp_id:
            session["blocker_under_review"] = None
            session["blocker_retry_count"] = 0

        msg["verdict"] = "SKIPPED"
        session["current_speaker"] = None
        session["status"] = "running"
        _append_system_message(
            session,
            f"⏭ 已将 [{emp_name}] 跳过：它不再占用本次会议的发言位，余下成员照常推进。\n"
            "配置与历史发言都保留，在「参会成员」面板点「恢复」即可把它拉回来。"
        )
        save_session(session)

    request_resume(session_id)
    return jsonify({"success": True, "session": get_session(session_id) or {}})


@app.route("/api/sessions/<session_id>/members/<emp_id>/restore", methods=["POST"])
def api_restore_member(session_id, emp_id):
    """把被跳过的成员放回发言位（下一轮/下一位审阅时重新点名）。"""
    if not is_valid_session_id(session_id):
        return jsonify({"error": "非法 session_id"}), 400
    if not _require_action_header():
        return jsonify({"error": "缺少操作确认头"}), 400

    with session_lock(session_id):
        session = get_session(session_id)
        if not session:
            return jsonify({"error": "会话不存在"}), 404
        session["skipped_employees"] = [
            e for e in (session.get("skipped_employees") or []) if e != emp_id
        ]
        restored = MeetingEngine.get_employee(load_settings(), emp_id)
        _append_system_message(
            session,
            f"↩️ 已把 [{restored.get('name') or emp_id}] 恢复到发言位，会在下一次点名时参与。",
        )
        save_session(session)

    return jsonify({"success": True, "session": get_session(session_id) or {}})


# ----------------- 服务生命周期（状态 / 重启 / 停止） -----------------
# werkzeug reloader 把「退出码 3」视为“请重新拉起子进程”，据此实现热重启
_RELOADER_RESTART_CODE = 3

# 这类接口能杀掉整个服务，必须挡住跨站表单提交：
# 自定义请求头无法由 <form> 或跨域 JS 直接发出（会触发预检且本服务不返回 CORS 头）
_ACTION_HEADER = "X-Meeting-Action"


def _require_action_header() -> bool:
    return request.headers.get(_ACTION_HEADER) == "1"


def _active_session_ids() -> List[str]:
    with _RUNNER_LOCK:
        return sorted(_ACTIVE_SESSIONS)


def _prepare_shutdown(action: str = "shutdown") -> int:
    """退出前收尾：终止所有在跑的 CLI 子进程，并把 running 的会议落盘为 paused。"""
    killed = 0
    try:
        killed = Executor.kill_all_processes()
    except Exception as e:
        print("[Service] 终止 CLI 子进程失败:", e)

    notice_tpl = ("🔄 服务已重启，本次发言被打断，会议转为暂停{where}。\n"
                  "点「▶️ 恢复讨论」继续即可；若连续两次都停在同一人，系统会自动暂缓它。"
                  if action == "restart" else
                  "⏻ 服务已停止，本次发言被打断{where}。\n"
                  "重新启动服务后点「▶️ 恢复讨论」继续；若连续两次都停在同一人，系统会自动暂缓它。")
    try:
        for item in list_sessions():
            sid = item.get("id")
            if not sid:
                continue
            with session_lock(sid):
                s = get_session(sid)
                if s and s.get("status") == "running":
                    speaker = (s.get("current_speaker") or "").strip()
                    s["status"] = "paused"
                    s["current_speaker"] = None
                    s.setdefault("messages", []).append({
                        "id": generate_msg_id(),
                        "sender": "system",
                        "sender_name": "系统广播",
                        "content": notice_tpl.format(where=f"（停在【{speaker}】发言中途）" if speaker else ""),
                        "phase": s.get("phase", 1),
                        "verdict": "RECOVER",
                        "elapsed_time": 0,
                        "created_at": int(time.time()),
                        "sent_prompt": "",
                        "raw_response": ""
                    })
                    save_session(s)
    except Exception as e:
        print("[Service] 会话状态复位失败:", e)
    return killed


# 无 reloader 时的“继任者”：等本进程真正退出（端口释放）后再 exec 启动脚本，
# 否则新进程会和旧进程抢 5050 端口而启动失败
_SPAWN_WAIT_CODE = r"""
import os, sys, time
pid, target = int(sys.argv[1]), sys.argv[2]
for _ in range(600):
    try:
        os.kill(pid, 0)
    except OSError:
        break
    time.sleep(0.1)
os.execv(sys.executable, [sys.executable, target])
"""


def _spawn_successor() -> bool:
    """在无 reloader 场景下拉起一个脱离当前进程的新实例。"""
    entry = os.path.abspath(sys.argv[0] or "")
    if not entry.endswith(".py") or not os.path.exists(entry):
        # 不是 `python app.py` 启动的（例如 gunicorn / flask run），交给用户手动重启
        return False
    try:
        subprocess.Popen(
            [sys.executable, "-c", _SPAWN_WAIT_CODE, str(os.getpid()), entry],
            cwd=os.getcwd(),
            start_new_session=True,
        )
        return True
    except Exception as e:
        print("[Service] 拉起后继进程失败:", e)
        return False


def _schedule_exit(action: str, delay: float = 0.4) -> None:
    """先让 HTTP 响应完整返回，再在后台真正退出/重启。"""
    def _run():
        time.sleep(delay)
        _prepare_shutdown(action)
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass

        if action == "restart":
            if _UNDER_RELOADER:
                os._exit(_RELOADER_RESTART_CODE)
            if _spawn_successor():
                os._exit(0)
            print("[Service] 无 reloader 且无法自动拉起，请手动执行 ./run.sh", file=sys.stderr)
            os._exit(1)
        os._exit(0)

    threading.Thread(target=_run, daemon=True, name="service-exit").start()


@app.route("/api/system/status", methods=["GET"])
def api_system_status():
    try:
        sessions = list_sessions()
    except Exception:
        sessions = []
    return jsonify({
        "pid": os.getpid(),
        "started_at": int(_STARTED_AT),
        "uptime_seconds": int(time.time() - _STARTED_AT),
        "debug": bool(_DEBUG_ENV),
        "hot_reload": bool(_UNDER_RELOADER),
        "python": "%d.%d.%d" % sys.version_info[:3],
        "cwd": os.getcwd(),
        "session_count": len(sessions),
        "active_sessions": _active_session_ids(),
    })


@app.route("/api/system/restart", methods=["POST"])
def api_system_restart():
    if not _require_action_header():
        return jsonify({"error": "缺少操作确认头"}), 400
    _schedule_exit("restart")
    return jsonify({
        "success": True,
        "action": "restart",
        "hot_reload": bool(_UNDER_RELOADER),
        "message": "服务正在重启，正在执行的 AI 任务已终止，会议将转为暂停",
    })


@app.route("/api/system/shutdown", methods=["POST"])
def api_system_shutdown():
    if not _require_action_header():
        return jsonify({"error": "缺少操作确认头"}), 400
    _schedule_exit("shutdown")
    return jsonify({
        "success": True,
        "action": "shutdown",
        "message": "服务正在停止，重新启动请运行 ./run.sh",
    })


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
