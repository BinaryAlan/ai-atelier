import os
import sys
import subprocess
import threading
import time
import re
import mimetypes
from typing import List, Optional
from flask import Flask, request, jsonify, send_from_directory, send_file

from core.storage import (
    load_settings, save_settings,
    list_sessions, get_session, create_session, save_session, delete_session,
    is_valid_session_id, session_lock, SessionDeletedError
)
from core.executor import Executor, validate_cli_template, normalize_cli_tools
from core.tasks import (
    task_manager, parse_collab, parse_fanout,
    STAGE_EXEC, STAGE_REVIEW, STAGE_DELIVER,
)
import core.streaming as streaming
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

    # 读/写权限分开存。老配置没有 read_access 字段 => 视为有读权限，
    # 避免升级后所有成员突然读不到工作目录（表现为只回一句「我先读取一下」）。
    read_access = bool(raw.get("read_access", True))
    write_access = bool(raw.get("write_access", False))
    # 模板里残留的 --tools "" 会让人连产物都读不到：按权限就地修好再落库，
    # 这样界面上看到的模板 = 实际执行用的模板。
    cli_cmd = normalize_cli_tools(cli_cmd, read_access, write_access)

    return {
        "id": agent_id,
        "name": str(raw.get("name") or "").strip() or label,
        "avatar": str(raw.get("avatar") or "🤖")[:8],
        "avatar_color": str(raw.get("avatar_color") or "#3b82f6")[:32],
        "type": raw.get("type") if raw.get("type") in ("cli", "api") else "cli",
        "cli_cmd": cli_cmd,
        # 绑定的「基础模型 + 推理档位」：只用于界面回显与会话级覆盖，
        # 真正执行仍以 cli_cmd 为准，避免界面状态影响命令语义。
        "model_key": str(raw.get("model_key") or "")[:128],
        "reasoning": str(raw.get("reasoning") or "")[:32],
        "system_prompt": str(raw.get("system_prompt") or "")[:8000],
        "enabled": bool(raw.get("enabled", True)),
        "timeout": timeout,
        # 写权限由服务端按 CLI 类型注入对应开关（见 core.executor.apply_access_mode）：
        # 模板里手写 --dangerously-skip-permissions 也能生效，但 UI 开关是唯一可审计的入口。
        "write_access": write_access,
        "read_access": read_access,
    }, None


def _clean_member_models(raw) -> "tuple":
    """会话级「成员 → 推理档位」覆盖表。

    只存执行时真正需要的最小字段；cli_cmd 走和成员配置同样的白名单校验，
    会话文件虽然在本机，也不能让它变成绕过 CLI 校验的旁路。
    """
    if not isinstance(raw, dict):
        return None, "member_models 必须为对象"
    cleaned = {}
    for emp_id, item in raw.items():
        if not isinstance(emp_id, str) or not AGENT_ID_REGEX.match(emp_id):
            return None, f"member_models 的成员 id 不合法: {emp_id}"
        if not isinstance(item, dict):
            return None, f"{emp_id} 的推理配置必须为对象"
        cli_cmd = str(item.get("cli_cmd") or "").strip()
        ok, err = validate_cli_template(cli_cmd)
        if not ok:
            return None, f"{emp_id} 的 CLI 命令不合法: {err}"
        cleaned[emp_id] = {
            "model_key": str(item.get("model_key") or "")[:128],
            "reasoning": str(item.get("reasoning") or "")[:32],
            "cli_cmd": cli_cmd,
        }
        if len(cleaned) > 100:
            return None, "member_models 条目过多"
    return cleaned, None


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

        # 会话内单独给某位成员挑的推理程度：只影响这一场会话
        if "member_models" in data:
            cleaned, err = _clean_member_models(data.get("member_models"))
            if err:
                return jsonify({"error": err}), 400
            session["member_models"] = cleaned

        save_session(session)
        return jsonify(session)

@app.route("/api/sessions/<session_id>", methods=["DELETE"])
def api_delete_session(session_id):
    if not is_valid_session_id(session_id):
        return jsonify({"error": "非法 session_id"}), 400

    # 任务系统的执行键是 `{session_id}:{task_id}:{stage}:{member_id}`，
    # 只按 session_id 精确匹配会漏杀正在执行的 CLI 子进程
    Executor.kill_session_processes(session_id)
    streaming.clear_session(session_id)
    success = delete_session(session_id)
    return jsonify({"success": success})


@app.route("/api/sessions/<session_id>/messages/<msg_id>", methods=["DELETE"])
def api_delete_message(session_id, msg_id):
    """从会话里删掉一条消息（聊天里右键 X 就调这里）。

    上下文（build_context）直接从 messages 里取，物理删掉即彻底不装载：
    之后发起的任务不会再把这条内容带进 Prompt。正在执行的任务不受影响，
    进程继续跑、产物照旧落盘，只是这条消息不再出现在对话里。
    """
    if not is_valid_session_id(session_id):
        return jsonify({"error": "非法 session_id"}), 400
    if not msg_id or len(msg_id) > 80:
        return jsonify({"error": "非法消息 id"}), 400

    try:
        with session_lock(session_id):
            session = get_session(session_id)
            if not session:
                return jsonify({"error": "会话不存在"}), 404

            messages = session.get("messages") or []
            remain = [
                m for m in messages
                if not (isinstance(m, dict) and m.get("id") == msg_id)
            ]
            if len(remain) == len(messages):
                return jsonify({"error": "消息不存在"}), 404

            session["messages"] = remain
            save_session(session)
    except SessionDeletedError:
        return jsonify({"error": "会话已删除"}), 404

    return jsonify({"success": True, "remaining": len(remain)})

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

# 允许在预览面板里直接编辑的文件类型：只放行纯文本，二进制（docx/图片/压缩包）
# 一旦被当文本写回去会直接损坏，所以用白名单而不是黑名单。
EDITABLE_EXTS = {
    ".md", ".markdown", ".mdx", ".txt", ".text", ".log",
    ".json", ".jsonl", ".yml", ".yaml", ".toml", ".ini", ".conf", ".cfg",
    ".csv", ".tsv", ".html", ".htm", ".css", ".scss", ".less",
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
    ".py", ".sh", ".bash", ".zsh", ".sql", ".xml", ".java", ".go", ".rs", ".rb", ".php",
}
# 在线编辑的体量上限：预览是 500KB，写回留一点余量但不无限放大
EDITABLE_MAX_BYTES = 2 * 1024 * 1024


def _file_meta(fpath: str) -> dict:
    """文件的「版本指纹」：mtime 精确到纳秒级（float），前端据此判断要不要重新读。"""
    st = os.stat(fpath)
    return {
        "path": fpath,
        "name": os.path.basename(fpath),
        "size": st.st_size,
        "mtime": st.st_mtime,
    }


@app.route("/api/workspace/file", methods=["GET"])
def api_workspace_file_content():
    fpath = request.args.get("path", "").strip()
    if not fpath or not is_path_safe(fpath) or not os.path.exists(fpath) or not os.path.isfile(fpath):
        return jsonify({"error": "文件不存在或无权访问"}), 404

    meta = _file_meta(fpath)
    # meta=1：只回版本指纹，不读正文。预览面板的「有没有被改写」轮询走这条路，
    # 一本几百 KB 的文档也不至于每秒读一遍盘。
    if request.args.get("meta") in ("1", "true"):
        return jsonify(meta)

    size = meta["size"]
    if size > 500 * 1024:
        # 明确告诉前端「为什么没有内容」，别让它把提示语当成文件正文渲染
        return jsonify({
            **meta, "content": "",
            "too_large": True,
            "error": "文件过大（>500KB），暂不支持在线预览",
        })

    try:
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return jsonify({**meta, "content": content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/workspace/file", methods=["POST"])
def api_workspace_file_save():
    """把编辑后的正文写回磁盘（预览面板的「编辑 → 保存」）。

    - 只允许纯文本白名单后缀，二进制文件一律拒绝（写回去就是损坏）
    - 写入走「同目录临时文件 + os.replace」，中途失败不会留下半个文件
    - 带 base_mtime 时做一次冲突检测：期间被 AI 或别的编辑器改过就返回 409，
      由前端确认「仍要覆盖」后再带 force=1 重发，避免悄悄盖掉刚写好的内容
    """
    data = request.get_json(silent=True) or {}
    fpath = str(data.get("path") or "").strip()
    content = data.get("content")
    if not fpath or not is_path_safe(fpath) or not os.path.exists(fpath) or not os.path.isfile(fpath):
        return jsonify({"error": "文件不存在或无权写入"}), 404
    if not isinstance(content, str):
        return jsonify({"error": "内容必须是文本"}), 400

    ext = os.path.splitext(fpath)[1].lower()
    if ext not in EDITABLE_EXTS:
        return jsonify({"error": "这类文件不支持在线编辑（只支持纯文本）"}), 415
    if "\x00" in content:
        return jsonify({"error": "内容里含有不可写入的二进制字符"}), 400
    encoded = content.encode("utf-8")
    if len(encoded) > EDITABLE_MAX_BYTES:
        return jsonify({"error": "内容过大（上限 2MB），请在本机编辑器里修改"}), 413

    meta = _file_meta(fpath)
    base_mtime = data.get("base_mtime")
    force = bool(data.get("force"))
    if isinstance(base_mtime, (int, float)) and not force:
        # 1ms 容差：纳秒级 mtime 在不同文件系统上可能有极小抖动
        if abs(meta["mtime"] - float(base_mtime)) > 0.001:
            return jsonify({
                "error": "文件在你编辑期间被改写了（可能是 AI 刚写入）",
                "code": "conflict",
                "mtime": meta["mtime"],
                "size": meta["size"],
            }), 409

    tmp_path = f"{fpath}.aim-save-{int(time.time() * 1000)}.tmp"
    try:
        mode = os.stat(fpath).st_mode & 0o777
        with open(tmp_path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, fpath)
    except Exception as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return jsonify({"error": f"保存失败：{e}"}), 500

    return jsonify({"ok": True, **_file_meta(fpath)})


# 预览弹窗里能直接看图：只放行图片类型，避免这个接口变成任意文件下载器
PREVIEW_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"}
PREVIEW_IMAGE_MAX = 20 * 1024 * 1024


@app.route("/api/workspace/raw", methods=["GET"])
def api_workspace_file_raw():
    fpath = request.args.get("path", "").strip()
    if not fpath or not is_path_safe(fpath) or not os.path.exists(fpath) or not os.path.isfile(fpath):
        return jsonify({"error": "文件不存在或无权访问"}), 404
    ext = os.path.splitext(fpath)[1].lower()
    if ext not in PREVIEW_IMAGE_EXTS:
        return jsonify({"error": "该类型不支持直接预览"}), 415
    if os.path.getsize(fpath) > PREVIEW_IMAGE_MAX:
        return jsonify({"error": "图片过大（>20MB），暂不支持在线预览"}), 413
    mime = mimetypes.guess_type(fpath)[0] or "application/octet-stream"
    if ext == ".svg":
        # 用 <img> 加载时脚本不会执行；这里再显式声明，避免被当成 HTML 打开
        mime = "image/svg+xml"
    return send_file(fpath, mimetype=mime, max_age=0)

@app.route("/api/sessions/<session_id>/live", methods=["GET"])
def api_session_live(session_id):
    """正在生成的文本（进程内暂存，不落库）。

    为什么要单独一个轻量接口：会话轮询是 1.5s 一次、且整份 messages 会被替换重渲染，
    拿它来做「逐字增长」既慢又贵。这里只回当前几条在跑的文本，前端 300~700ms 拉一次，
    回答结束后这些 key 会消失，正式结果照常从 /api/sessions/<id> 取。
    """
    if not is_valid_session_id(session_id):
        return jsonify({"error": "非法 session_id"}), 400
    if not get_session(session_id):
        return jsonify({"error": "会话不存在"}), 404
    return jsonify({"streams": streaming.for_session(session_id)})


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
        # 任务已结束 / 环节已结束 → 409（可重试的冲突），不是「找不到」
        return jsonify(result), 409 if result.get("code") else 404
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


@app.route("/api/mentions/scan", methods=["POST"])
def api_scan_mentions():
    """输入时的「这条会发给谁」预览：一条消息 @ 多位时前端据此提示并行分工。

    谁被 @ 到了只有服务端说了算（支持别名、`@//agy/` 这种路径写法、散落在句子里），
    前端不再自己写一套匹配逻辑。
    """
    content = str(_json_body().get("content") or "")
    settings = load_settings()
    owners, error = parse_fanout(content, settings)
    names = []
    for oid in owners:
        emp = next((e for e in settings.get("employees") or []
                    if isinstance(e, dict) and e.get("id") == oid), None)
        names.append((emp or {}).get("name") or oid)
    return jsonify({
        "fanout": len(owners) >= 2 and not error,
        "names": names,
        "error": error,
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
import os, sys, time, traceback
pid, target = int(sys.argv[1]), sys.argv[2]
print("[restart] %s 继任进程已启动，等待旧进程 %d 退出" % (time.strftime("%Y-%m-%d %H:%M:%S"), pid), flush=True)
for _ in range(600):
    try:
        os.kill(pid, 0)
    except OSError:
        break
    time.sleep(0.1)
else:
    print("[restart] 60 秒内旧进程仍未退出，放弃接管（端口可能仍被占用）", flush=True)
    sys.exit(3)
try:
    os.execv(sys.executable, [sys.executable, target])
except Exception:
    print("[restart] 拉起新实例失败：\n" + traceback.format_exc(), flush=True)
    sys.exit(4)
"""

# 继任进程的输出去处：不继承旧进程的终端/管道（见 _spawn_successor 注释），统一写日志文件
_SERVICE_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".run", "service.log")


def _spawn_successor() -> bool:
    """在无 reloader 场景下拉起一个脱离当前进程的新实例。

    关键：继任进程的 stdin/stdout/stderr 一律重定向，**不能继承父进程的**。
    父进程常由终端或后台管理器拉起，它一退出，那根管道/伪终端的读端也跟着消失；
    继任进程再往 fd 1 写第一个 print（启动横幅）就会拿到 EPIPE/EIO，
    Python 于是带着退出码 120 直接死掉 —— 旧进程已 os._exit，端口没人接，
    表现就是 `./run.sh restart` 干等 30 秒超时。
    """
    entry = os.path.abspath(sys.argv[0] or "")
    if not entry.endswith(".py") or not os.path.exists(entry):
        # 不是 `python app.py` 启动的（例如 gunicorn / flask run），交给用户手动重启
        return False
    log = None
    try:
        os.makedirs(os.path.dirname(_SERVICE_LOG), exist_ok=True)
        # 简单轮转：超过 2MB 就留一代历史，避免长期重启把日志撑爆
        try:
            if os.path.getsize(_SERVICE_LOG) > 2 * 1024 * 1024:
                os.replace(_SERVICE_LOG, _SERVICE_LOG + ".1")
        except OSError:
            pass
        log = open(_SERVICE_LOG, "ab", buffering=0)
        log.write(
            ("\n[restart] %s 旧进程 pid=%d 拉起继任实例\n"
             % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid())).encode("utf-8")
        )
        subprocess.Popen(
            [sys.executable, "-c", _SPAWN_WAIT_CODE, str(os.getpid()), entry],
            cwd=os.getcwd(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return True
    except Exception as e:
        print("[Service] 拉起后继进程失败:", e)
        return False
    finally:
        if log is not None:
            log.close()  # 子进程已持有自己的 fd，父进程这份可以关


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
        "log_file": _SERVICE_LOG,
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

    # 由 /api/system/restart 拉起的继任实例 PID 已换，顺手刷新 PID 文件，
    # 免得 ./run.sh status / stop 读到过期进程号（正常 start 时值与 run.sh 写的相同）
    try:
        _pid_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".run", "app.pid")
        os.makedirs(os.path.dirname(_pid_file), exist_ok=True)
        with open(_pid_file, "w") as _f:
            _f.write(str(os.getpid()))
    except Exception:
        pass

    # use_debugger=False 彻底禁止外部或局域网访问 Werkzeug Debugger PIN 执行任意代码
    app.run(host=host, port=port, debug=is_debug, use_reloader=is_debug, use_debugger=False)
