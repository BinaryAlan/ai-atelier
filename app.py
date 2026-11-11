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
from core.executor import Executor, validate_cli_template
from core.tasks import task_manager, parse_collab, STAGE_EXEC, STAGE_REVIEW, STAGE_DELIVER
from core.scanner import scan_local_models

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# 启动时刻与热重载开关（供 /api/system/status 展示）
_STARTED_AT = time.time()
_DEBUG_ENV = os.environ.get("FLASK_DEBUG", "0") in ["1", "true"]
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


@app.after_request
def _cache_policy(resp):
    """静态资源全部本地化后的缓存策略。

    - static/vendor/*：第三方库固定版本且 URL 带 ?v=，可长期强缓存（immutable）。
    - 其余 static/*：交给 ETag / Last-Modified 走 304 协商缓存，改完刷新即生效，
      不会像长 TTL 那样把开发中的改动卡在浏览器里。
    - 页面与所有 /api/*：一律不缓存，避免轮询拿到旧状态。
    """
    path = request.path or ""
    if path.startswith("/static/vendor/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    elif path == "/" or path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def _json_body() -> dict:
    """安全读取 JSON 请求体：非 JSON / 空 body 一律返回 {}，不抛 415。"""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _clean_employee_ids(raw) -> Optional[List[str]]:
    """校验并规范化会话成员 id 列表（只保留字符串、去重、剔除未知成员）。"""
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
    # 0 = 不限制（默认）：模型可以跑到自己结束，需要中断时在对应消息或卡片上点取消
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
        # 写权限由服务端按 CLI 类型注入对应开关（见 core.executor.apply_access_mode）：
        # 模板里手写 --dangerously-skip-permissions 也能生效，但 UI 开关是唯一可审计的入口。
        "write_access": bool(raw.get("write_access", False)),
    }, None


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    data = _json_body()

    # 设置只有成员与界面语言两部分，其余字段一律不接受，避免旧数据重新混进配置
    employees_raw = data.get("employees", [])
    if not isinstance(employees_raw, list):
        return jsonify({"error": "employees 必须为列表"}), 400

    employees = []
    seen_ids = set()
    for idx, emp in enumerate(employees_raw):
        cleaned, err = _sanitize_agent(emp, f"第 {idx + 1} 个员工", f"emp_{idx + 1}")
        if err:
            return jsonify({"error": err}), 400
        if cleaned["id"] in seen_ids:
            return jsonify({"error": f"成员 id 重复: {cleaned['id']}"}), 400
        seen_ids.add(cleaned["id"])
        employees.append(cleaned)

    lang = str(data.get("ui_language") or "zh").lower()
    payload = {
        "ui_language": lang if lang in ("zh", "en") else "zh",
        "employees": employees,
    }

    save_settings(payload)
    return jsonify({"success": True, "settings": payload})

# ----------------- Sessions API -----------------
@app.route("/api/sessions", methods=["GET"])
def api_list_sessions():
    return jsonify(list_sessions())

@app.route("/api/sessions", methods=["POST"])
def api_create_session():
    data = _json_body()
    title = str(data.get("title") or "").strip() or "新会话"
    title = title[:200]

    # 传 None 时由 storage 使用「默认启用名单」
    employee_ids = _clean_employee_ids(data.get("employee_ids"))

    workspace_dir = data.get("workspace_dir")
    if not isinstance(workspace_dir, str) or not is_path_safe(workspace_dir) or not os.path.isdir(os.path.expanduser(workspace_dir)):
        workspace_dir = os.getcwd()

    session = create_session(
        title=title,
        employee_ids=employee_ids,
        workspace_dir=workspace_dir,
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
            session["employee_ids"] = new_ids

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
        return jsonify({"error": "消息内容不能为空"}), 400
    if len(content) > 20000:
        return jsonify({"error": "消息内容过长（上限 20000 字符）"}), 400

    # 「💬 回复」某条消息：前端只传被引用消息的 id，引用对象与定向目标一律以服务端
    # 存档为准（不信任客户端传来的作者/正文），避免伪造引用去骗某个模型。
    reply_to = data.get("reply_to")
    reply_id = ""
    if isinstance(reply_to, dict):
        reply_id = str(reply_to.get("id") or "").strip()
    elif isinstance(reply_to, str):
        reply_id = reply_to.strip()

    settings = load_settings()
    with session_lock(session_id):
        session = get_session(session_id)
        if not session:
            return jsonify({"error": "会话不存在"}), 404

        forced_owner_id = None
        if reply_id:
            quoted = _find_message(session, reply_id)
            if not quoted:
                return jsonify({"error": "被回复的消息不存在，可能已被删除"}), 404
            sender = quoted.get("sender")
            if sender in ("user", "system"):
                return jsonify({"error": "只能回复 AI 成员的消息"}), 400
            for emp in settings.get("employees") or []:
                if isinstance(emp, dict) and emp.get("id") == sender:
                    forced_owner_id = sender
                    break
            if not forced_owner_id:
                return jsonify({"error": "该成员已不在成员列表中，无法直接回复"}), 400

    # 任务系统接管：@ 单任务 / /collab 协助任务；没点名的消息只作为上下文保留
    result = task_manager.submit_message(
        session_id, content, settings,
        reply_to=reply_id or None,
        forced_owner_id=forced_owner_id,
    )
    if result.get("error"):
        return jsonify({"error": result["error"], "validation_error": bool(result.get("validation_error"))}), 400

    return jsonify(get_session(session_id) or {})

@app.route("/api/sessions/<session_id>/tasks/<tid>/cancel", methods=["POST"])
def api_cancel_task(session_id, tid):
    """静默取消某个执行环节：终止进程、状态转已取消，不追加系统消息。"""
    if not is_valid_session_id(session_id):
        return jsonify({"error": "非法 session_id"}), 400
    data = _json_body()
    stage = str(data.get("stage") or STAGE_EXEC)
    if stage not in (STAGE_EXEC, STAGE_REVIEW, STAGE_DELIVER):
        return jsonify({"error": "非法环节"}), 400
    member_id = str(data.get("member_id") or "")
    result = task_manager.cancel(session_id, tid, stage, member_id)
    if result.get("error"):
        return jsonify(result), 404
    return jsonify(result)


@app.route("/api/sessions/<session_id>/tasks/<tid>/retry", methods=["POST"])
def api_retry_task(session_id, tid):
    """重跑被点击的环节：不新增任务、不增加流程轮次。"""
    if not is_valid_session_id(session_id):
        return jsonify({"error": "非法 session_id"}), 400
    data = _json_body()
    stage = str(data.get("stage") or STAGE_EXEC)
    if stage not in (STAGE_EXEC, STAGE_REVIEW, STAGE_DELIVER):
        return jsonify({"error": "非法环节"}), 400
    member_id = str(data.get("member_id") or "")
    result = task_manager.retry(session_id, tid, stage, member_id)
    if result.get("error"):
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/collab/validate", methods=["POST"])
def api_validate_collab():
    """输入时的 /collab 严格校验：缺主创或缺协助成员直接拦截提示。"""
    content = str(_json_body().get("content") or "")
    parsed = parse_collab(content, load_settings())
    return jsonify({
        "ok": bool(parsed["ok"]),
        "error": parsed.get("error", ""),
        "owner_id": parsed.get("owner_id", ""),
        "reviewer_ids": parsed.get("reviewer_ids", []),
        "goal": parsed.get("goal", ""),
    })


def _find_message(session: dict, msg_id: str) -> Optional[dict]:
    for m in session.get("messages") or []:
        if isinstance(m, dict) and m.get("id") == msg_id:
            return m
    return None


# ----------------- 服务生命周期（状态 / 重启 / 停止） -----------------
# werkzeug reloader 把「退出码 3」视为“请重新拉起子进程”，据此实现热重启
_RELOADER_RESTART_CODE = 3

# 这类接口能杀掉整个服务，必须挡住跨站表单提交：
# 自定义请求头无法由 <form> 或跨域 JS 直接发出（会触发预检且本服务不返回 CORS 头）
_ACTION_HEADER = "X-Meeting-Action"


def _require_action_header() -> bool:
    return request.headers.get(_ACTION_HEADER) == "1"


def _prepare_shutdown(action: str = "shutdown") -> int:
    """退出前收尾：终止所有在跑的 CLI 子进程。

    进行中的任务不在这里改写状态：下次启动时 recover_on_startup() 会统一标记为已取消，
    用户可在卡片里从被中断的环节重试。
    """
    try:
        return Executor.kill_all_processes()
    except Exception as e:
        print("[Service] 终止 CLI 子进程失败:", e)
        return 0


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

    active_tasks = []
    for item in sessions:
        sid = item.get("id") if isinstance(item, dict) else None
        if not sid:
            continue
        full = get_session(sid)
        for tid, task in (full or {}).get("tasks", {}).items():
            if isinstance(task, dict) and task.get("status") in ("running", "queued"):
                active_tasks.append(tid)

    return jsonify({
        "pid": os.getpid(),
        "started_at": int(_STARTED_AT),
        "uptime_seconds": int(time.time() - _STARTED_AT),
        "debug": bool(_DEBUG_ENV),
        "hot_reload": bool(_UNDER_RELOADER),
        "python": "%d.%d.%d" % sys.version_info[:3],
        "cwd": os.getcwd(),
        "session_count": len(sessions),
        "active_tasks": active_tasks,
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
        "message": "服务正在重启，正在执行的任务已终止，重启后可在卡片里重试",
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


# 服务启动 / 重启：进行中的任务标记已取消（可从被中断环节重试），排队任务重新拉起 worker
try:
    _recovered_tasks = task_manager.recover_on_startup()
    if _recovered_tasks:
        print(f"[Tasks] 服务重启：{_recovered_tasks} 个进行中的任务已标记为已取消")
except Exception as _recover_exc:
    print("[Tasks] 启动恢复失败:", _recover_exc)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    host = os.environ.get("HOST", "127.0.0.1")  # 默认仅绑定本地，杜绝局域网调试器暴露
    is_debug = os.environ.get("FLASK_DEBUG", "0") in ["1", "true"]

    print(f"\n🚀 AI-Meeting（多 AI 协同任务工作台）启动中...")
    print(f"👉 访问地址: http://127.0.0.1:{port}")
    if is_debug:
        print(f"⚡ 开发模式：代码热重载已启用 (无交互调试器暴露，安全模式)")
    else:
        print(f"ℹ️  正常使用：改 .py 后执行 ./run.sh restart；开发时可用 ./run.sh dev 开启热重载")

    # use_debugger=False 彻底禁止外部或局域网访问 Werkzeug Debugger PIN 执行任意代码
    app.run(host=host, port=port, debug=is_debug, use_reloader=is_debug, use_debugger=False)
