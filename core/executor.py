import os
import random
import re
import signal
import subprocess
import time
import shlex
import threading
from typing import Dict, Any, Tuple, Optional, List
from urllib.parse import urlsplit
from dataclasses import dataclass

# 白名单只允许「AI CLI 客户端」。
# 严禁加入 python/python3/bash/sh 等通用解释器：它们带 -c/-m 参数即可执行任意代码，
# 会让 validate_cli_template 与路径校验形同虚设（等价于本地 RCE）。
ALLOWED_BINARIES = {"agy", "codebuddy"}

# 单条 prompt 的长度上限：prompt 通过 argv 传递，超长会触发 E2BIG 并让 CLI 直接失败
MAX_PROMPT_CHARS = 100_000
MAX_TIMEOUT = 86400    # 有上限时最多 24 小时，防止配置写错变成天文数字
# 超时语义：0 或空 = 不限制（详见 _coerce_timeout）

# 模型把「工具调用」或思考链当正文吐出来时的产物（CodeBuddy 在 --tools "" 下尤其明显，
# 或 DeepSeek 等推理模型外溢的 think 块）。这些不是任务输出，必须剥掉，避免干扰结果解析。
_TOOL_TAGS = (
    "tool_calls", "system_calls", "function_calls", "tool_use",
    "tool_reminder", "system_reminder", "system_prompt",
    "invoke", "parameter", "result",
    "thought", "think", "antthinking",
)


def sanitize_model_output(text: str) -> str:
    """
    剥除残留的工具调用 XML / 系统提醒块，返回干净正文。

    只在「整块」出现时剥离；未闭合的标签视为尾部垃圾一并截断
    （模型在工具不可用时经常先吐半个 <tool_calls> 就结束本轮）。
    """
    if not text:
        return ""

    cleaned = text
    for tag in _TOOL_TAGS:
        cleaned = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", "", cleaned, flags=re.S | re.I)
        cleaned = re.sub(rf"<{tag}\b[^>]*>.*\Z", "", cleaned, flags=re.S | re.I)
        cleaned = re.sub(rf"</?{tag}\b[^>]*>", "", cleaned, flags=re.I)

    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


@dataclass
class ExecutionResult:
    status: str       # "success" | "timeout" | "interrupted" | "error"
    output: str       # Cleaned text output or error summary
    raw_output: str   # Exact stdout / stderr
    elapsed: float    # Elapsed seconds
    error_msg: Optional[str] = None


_PROCESS_LOCK = threading.Lock()
ACTIVE_PROCESSES: Dict[str, List[subprocess.Popen]] = {}

# CLI 启动时要自己挑一个本地端口起服务（codebuddy 会监听 127.0.0.1:<随机>）。
# 「探测空闲端口」和「真正 listen」之间有窗口期，并发起多个实例时仍可能撞上同一个端口，
# 进程还没干活就因 EADDRINUSE 退出。属于瞬时错误，重试一次即可（下次会挑别的端口）。
#
# 注意：更常见的 EADDRINUSE 不是并发碰撞，而是环境变量被继承（见 build_cli_env）。
_PORT_CONFLICT_HINTS = ("EADDRINUSE", "address already in use")
CLI_PORT_CONFLICT_RETRIES = 2

# ---------------------------------------------------------------------------
# 起 CLI 子进程时必须在干净环境里跑，不能全量继承宿主进程的环境变量。
#
# 起因（实测）：WorkBuddy / IDE 会把自己的本地服务端口以 SERVER__PORT 注入到它拉起的
# 所有子进程里。而 codebuddy CLI 启动时是这么取端口的：
#     parseInt(process.env.SERVER__PORT) || config.cell.server.port
# 也就是说「环境里有就直接用，不再挑空闲端口」。于是从 IDE 终端启动本服务后，
# 每个 CLI 子进程都去 listen 宿主自己的端口（例如 127.0.0.1:62587，宿主 Electron 正在监听），
# 必定 EADDRINUSE，2 秒就退出——不是偶发碰撞，是 100% 复现。
#
# 同理，CODEBUDDY_SESSION_* / CODEBUDDY_DAEMON_* / CODEBUDDY_GATEWAY_* 这些会让 CLI 以为
# 自己是宿主的内嵌会话，转而向宿主索取凭据，结果是「Authentication required」，
# 反而读不到 CLI 自己的登录态。
#
# 为什么用白名单而不是黑名单：宿主注入的变量实测有 81 个（CODEBUDDY_*、WORKBUDDY_*、
# CLIENT_INFO_*、CLAUDE_*、SERVER__*、沙箱代理……），且随宿主版本变化。先前按前缀删，
# 仍然漏掉了 CLIENT_INFO_* 与 CLAUDE_* 的组合——它们凑齐后 CLI 判定「我在 IDE 里」，
# 改走 IDE 凭据通道，于是随机报 Authentication required。黑名单永远防不住下一个新变量，
# 所以反过来只放行确实需要的那些。
#
# 实测对照（同一台机器、同一条命令）：
#   继承环境                → 2.1s 失败，EADDRINUSE 127.0.0.1:62587
#   按前缀删（黑名单）       → 单跑时好时坏，两个并发必失败
#   只放行白名单            → 单跑 4~8s 正常；并发 2 个 × 3 轮全部成功
# ---------------------------------------------------------------------------
# 只放行「进程跑起来必需」+「用户自己配的凭据」。名单之外一律不带过去。
_CLI_ENV_ALLOW = {
    # 运行必需
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL", "USER", "LOGNAME",
    "TMPDIR", "PWD", "TZ", "SSH_AUTH_SOCK",
    # Windows
    "SYSTEMROOT", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
    # 用户自己配的凭据 / 服务地址：不带过去 CLI 会连不上
    "CODEBUDDY_API_KEY", "CODEBUDDY_BASE_URL", "CODEBUDDY_CONFIG_DIR",
    "CLAUDE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
}

# 代理单独处理：宿主会把自己的沙箱代理（实测 127.0.0.1:62616，属于 sandbox-client 进程）
# 塞进 HTTP_PROXY/HTTPS_PROXY，让所有子进程的网络请求经它转发，CLI 走它就会时好时坏。
# 只放行指向外部的代理；公司/外网代理（10.x、proxy.corp.com）要保留，否则连不上网。
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
               "http_proxy", "https_proxy", "all_proxy", "no_proxy")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _is_loopback_proxy(value: str) -> bool:
    """代理地址是否指向本机（宿主注入的沙箱代理都是环回地址）。"""
    raw = (value or "").strip()
    if not raw:
        return False
    if "//" not in raw:
        raw = "//" + raw
    try:
        host = urlsplit(raw).hostname
    except ValueError:
        return False
    return (host or "").lower() in _LOOPBACK_HOSTS


def build_cli_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """构造 CLI 子进程的环境：只放行白名单，其余宿主变量一律不传。"""
    src = os.environ if base is None else base
    env: Dict[str, str] = {}
    keep_loopback_proxy = os.environ.get("AIMEET_KEEP_LOOPBACK_PROXY") == "1"

    for key, value in src.items():
        if key in _CLI_ENV_ALLOW:
            env[key] = value
        elif key in _PROXY_KEYS:
            # 外部代理保留；环回代理默认视为宿主注入，除非用户显式要求保留
            if keep_loopback_proxy or not _is_loopback_proxy(value):
                env[key] = value

    # 少数 CLI 在 TERM 缺失时不输出彩色/进度，给个默认值避免行为差异
    env.setdefault("TERM", "xterm-256color")
    return env


def is_port_conflict(text: str) -> bool:
    return any(hint in (text or "") for hint in _PORT_CONFLICT_HINTS)


def _unregister_process(session_id: str, proc: Optional[subprocess.Popen]) -> None:
    """把进程从活动表里摘掉（取消时按这张表找进程杀）。"""
    if not proc:
        return
    with _PROCESS_LOCK:
        procs = ACTIVE_PROCESSES.get(session_id)
        if procs and proc in procs:
            procs.remove(proc)
            if not procs:
                ACTIVE_PROCESSES.pop(session_id, None)



def validate_cli_template(cmd_template: str) -> Tuple[bool, str]:
    """
    安全校验 CLI 执行模板：
    1. 必须以白名单命令开头 (agy, codebuddy)
    2. 必须包含 {prompt} 占位符
    3. 严禁包含危险的 Shell 管道、重定向、分号及命令拼接符
    """
    if not cmd_template or not cmd_template.strip():
        return False, "CLI 命令模板不能为空"

    # 危险字符检查
    dangerous_chars = [";", "&&", "||", "|", "`", "$(", ">", "<", "\n", "\r"]
    for ch in dangerous_chars:
        if ch in cmd_template:
            return False, f"CLI 命令模板禁止包含 Shell 管道与特殊连接符: {ch}"

    try:
        tokens = shlex.split(cmd_template)
    except Exception as e:
        return False, f"CLI 命令格式解析错误: {str(e)}"

    if not tokens:
        return False, "解析出的命令参数为空"

    base_cmd = os.path.basename(tokens[0]).lower()
    if base_cmd not in ALLOWED_BINARIES:
        return False, f"禁止执行未在白名单中的命令: '{base_cmd}'。允许的 CLI: {', '.join(sorted(ALLOWED_BINARIES))}"

    if "{prompt}" not in cmd_template:
        return False, "CLI 命令模板必须包含 '{prompt}' 占位符"

    return True, "验证通过"


# 写权限（写文件 / 跑命令）在两家 CLI 上是不同开关：
#   agy        --dangerously-skip-permissions   自动放行所有工具权限询问
#   codebuddy  -y / --dangerously-skip-permissions
# 光加开关还不够：codebuddy 的 --tools 会把可用工具裁成白名单，
# 只给了 Read,Glob,Grep 时即使放行权限也没有写的能力，必须把写工具补进去。
_WRITE_FLAGS = {
    "agy": ["--dangerously-skip-permissions"],
    "codebuddy": ["-y"],
}
# 缺一不可：只给 Write 不给 Edit，模型改现有文件时会退化成「重写整个文件」或直接失败
_WRITE_TOOLS = ("Read", "Glob", "Grep", "Write", "Edit", "Bash")


def apply_access_mode(args: List[str], write_access: bool) -> List[str]:
    """
    按角色的写权限给 CLI 参数补上对应开关（对同一份模板可重复调用，不会叠加）。

    write_access=False 时原样返回：只读角色不该因为这次改动突然获得写盘能力。
    """
    if not write_access or not args:
        return list(args)

    base = os.path.basename(args[0]).lower()
    out = list(args)

    for flag in _WRITE_FLAGS.get(base, []):
        if flag not in out and not (flag == "-y" and "--dangerously-skip-permissions" in out):
            out.append(flag)

    if base == "codebuddy" and "--tools" in out:
        idx = out.index("--tools")
        if idx + 1 < len(out):
            current = out[idx + 1].strip()
            if current == "":
                # --tools "" 等于把工具全禁掉：要写就得整体放开
                out[idx + 1] = "default"
            elif current.lower() not in ("default", "all"):
                have = [t.strip() for t in current.split(",") if t.strip()]
                lower = {t.lower() for t in have}
                for tool in _WRITE_TOOLS:
                    if tool.lower() not in lower:
                        have.append(tool)
                out[idx + 1] = ",".join(have)
    return out


def _coerce_timeout(value: Any, fallback: Optional[int] = None) -> Optional[int]:
    """
    把配置里的超时值规范成「秒数」或「None（不限制）」。

    - 缺省/空值 → 用 fallback（调用方传的默认值，也是 None 表示不限制）
    - 0 或负数 → None，即不设超时：模型想跑多久就跑多久，
      需要中断时由用户在对应消息或任务卡片上点「取消」。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return fallback
    try:
        timeout = int(float(value))
    except (TypeError, ValueError):
        return fallback
    if timeout <= 0:
        return None
    return min(MAX_TIMEOUT, max(1, timeout))


class Executor:
    @staticmethod
    def kill_session_process(session_id: str) -> bool:
        """
        强制终止当前会话正在运行的所有 CLI 进程组，并彻底回收僵尸进程。
        """
        killed_any = False
        with _PROCESS_LOCK:
            procs = ACTIVE_PROCESSES.pop(session_id, [])

        for p in procs:
            if p and p.poll() is None:
                # 打上显式中断标记，供 _run_cli 区分「用户取消」与「程序自身非零退出」
                try:
                    p.wb_interrupted = True  # type: ignore[attr-defined]
                except Exception:
                    pass
                try:
                    if os.name == "posix":
                        pgid = os.getpgid(p.pid)
                        os.killpg(pgid, signal.SIGTERM)
                    else:
                        p.terminate()
                    try:
                        p.wait(timeout=0.3)
                    except subprocess.TimeoutExpired:
                        if os.name == "posix":
                            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                        else:
                            p.kill()
                        p.wait(timeout=1.0)
                    killed_any = True
                except ProcessLookupError:
                    killed_any = True
                except Exception as e:
                    print(f"终止进程组异常 (PID: {p.pid}):", e)
        return killed_any

    @staticmethod
    def kill_session_processes(session_id: str) -> int:
        """
        终止某会话名下的全部 CLI 进程：既包括以 session_id 为键的调用，
        也包括任务系统的执行单元键（`{session_id}:{task_id}:{stage}:{member_id}`）。

        删除会话时必须走这个入口：只按 session_id 精确匹配会漏掉正在执行的任务，
        子进程会跑完、白烧 token，而且没人接收结果。
        """
        prefix = session_id + ":"
        with _PROCESS_LOCK:
            keys = [k for k in ACTIVE_PROCESSES if k == session_id or k.startswith(prefix)]
        killed = 0
        for key in keys:
            try:
                if Executor.kill_session_process(key):
                    killed += 1
            except Exception as e:
                print(f"终止会话 {key} 的进程组失败:", e)
        return killed

    @staticmethod
    def kill_all_processes() -> int:
        """
        终止所有会话正在运行的 CLI 进程组（服务停止/重启前调用）。
        不这么做的话，子进程会被 init 收养后继续跑完，白烧 token 且没人接收结果。
        """
        with _PROCESS_LOCK:
            session_ids = list(ACTIVE_PROCESSES.keys())
        killed = 0
        for sid in session_ids:
            try:
                if Executor.kill_session_process(sid):
                    killed += 1
            except Exception as e:
                print(f"终止会话 {sid} 的进程组失败:", e)
        return killed

    @staticmethod
    def run_employee(
        employee: Dict[str, Any],
        prompt: str,
        session_id: str = "default",
        workspace_dir: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> ExecutionResult:
        """
        调用一位 AI 成员，返回结构化 ExecutionResult。
        """
        start_time = time.time()
        emp_type = employee.get("type", "cli")
        emp_timeout = _coerce_timeout(employee.get("timeout", timeout), timeout)

        if emp_type == "cli":
            cmd_template = employee.get("cli_cmd", "")
            if not cmd_template:
                return ExecutionResult(
                    status="error",
                    output="【该成员未配置 CLI 命令模板，无法执行任务】",
                    raw_output="",
                    elapsed=0.0,
                    error_msg="missing cli_cmd"
                )
            result = Executor._run_cli(
                cmd_template=cmd_template,
                prompt=prompt,
                session_id=session_id,
                workspace_dir=workspace_dir,
                timeout=emp_timeout,
                write_access=bool(employee.get("write_access"))
            )
        elif emp_type == "api":
            result = ExecutionResult(
                status="error",
                output="【API 模式正在开发支持中】",
                raw_output="",
                elapsed=0.0,
                error_msg="API 模式暂未启用"
            )
        else:
            result = ExecutionResult(
                status="error",
                output=f"【错误: 未知的员工类型 {emp_type}】",
                raw_output="",
                elapsed=0.0,
                error_msg=f"Unknown employee type {emp_type}"
            )

        result.elapsed = round(time.time() - start_time, 2)

        # 清洗「工具调用被当成正文」的残留。raw_output 保留原样供排查。
        if result.status == "success" and result.output:
            result.output = sanitize_model_output(result.output)

        return result


    @staticmethod
    def _run_cli(
        cmd_template: str,
        prompt: str,
        session_id: str = "default",
        workspace_dir: Optional[str] = None,
        timeout: Optional[int] = None,
        write_access: bool = False
    ) -> ExecutionResult:
        """
        智能解析 CLI 命令模板，并在指定工作目录中独立进程组运行。
        """
        # 校验 CLI 模板合法性
        is_valid, err_msg = validate_cli_template(cmd_template)
        if not is_valid:
            return ExecutionResult(
                status="error",
                output=f"【CLI 安全校验未通过: {err_msg}】",
                raw_output="",
                elapsed=0.0,
                error_msg=err_msg
            )

        if len(prompt) > MAX_PROMPT_CHARS:
            return ExecutionResult(
                status="error",
                output=f"【Prompt 过长（{len(prompt)} 字符，上限 {MAX_PROMPT_CHARS}），已中止本次调用】",
                raw_output="",
                elapsed=0.0,
                error_msg="prompt too large for argv"
            )

        tokens = shlex.split(cmd_template)
        final_args = []
        replaced = False

        for token in tokens:
            if "{prompt}" in token:
                final_args.append(token.replace("{prompt}", prompt))
                replaced = True
            else:
                final_args.append(token)

        if not replaced:
            final_args.append(prompt)

        # 写权限只在真正起进程前的最后一步注入，模板本身保持干净（用户随时可改回只读）
        final_args = apply_access_mode(final_args, write_access)

        # 校验工作目录（必须是真实存在的目录）
        cwd = None
        if workspace_dir and os.path.isdir(workspace_dir):
            cwd = os.path.abspath(workspace_dir)
        else:
            cwd = os.getcwd()

        p = None
        try:
            popen_kwargs = dict(
                cwd=cwd,
                # 不清环境的话，CLI 会拿宿主注入的 SERVER__PORT 当自己的监听端口，启动即失败
                env=build_cli_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if os.name == "posix":
                # 独立进程组，便于整组回收子进程
                popen_kwargs["preexec_fn"] = os.setsid

            for attempt in range(1, CLI_PORT_CONFLICT_RETRIES + 1):
                p = subprocess.Popen(final_args, **popen_kwargs)
                with _PROCESS_LOCK:
                    ACTIVE_PROCESSES.setdefault(session_id, []).append(p)

                stdout, stderr = p.communicate(timeout=timeout)

                # 只有被 kill_session_process 显式标记过的才算「用户取消」
                # （否则 CLI 自己 exit(9)/exit(15) 会被误判为用户中断）
                if getattr(p, "wb_interrupted", False):
                    return ExecutionResult(
                        status="interrupted",
                        output="【任务已被用户取消】",
                        raw_output=(stderr or "").strip() or (stdout or "").strip(),
                        elapsed=0.0,
                        error_msg="Process interrupted by user cancellation"
                    )

                if p.returncode != 0:
                    err_text = (
                        (stderr or "").strip() or (stdout or "").strip()
                        or f"Process exited with code {p.returncode}"
                    )
                    conflict = is_port_conflict(err_text)
                    # 只在「一个字的产出都没有」时重试：进程启动就挂了，
                    # 重跑不会重复已经完成的工作
                    if (
                        conflict
                        and attempt < CLI_PORT_CONFLICT_RETRIES
                        and not (stdout or "").strip()
                    ):
                        # 加抖动：协助任务的审核是并行的，两个实例同时失败、
                        # 又同时醒来重试的话会再撞一次
                        delay = attempt + random.uniform(0, 0.8)
                        print(f"[Executor] CLI 端口被占用，{delay:.1f} 秒后重试: {err_text[:120]}")
                        _unregister_process(session_id, p)
                        p = None
                        time.sleep(delay)
                        continue
                    hint = (
                        f"（本机端口被占用，已自动重试 {CLI_PORT_CONFLICT_RETRIES} 次仍失败）"
                        if conflict else ""
                    )
                    return ExecutionResult(
                        status="error",
                        output=f"【CLI 执行报错{hint}: {err_text}】",
                        raw_output=f"Exit code: {p.returncode}\n{err_text}",
                        elapsed=0.0,
                        error_msg=err_text
                    )

                return ExecutionResult(
                    status="success",
                    output=(stdout or "").strip(),
                    raw_output=stdout or "",
                    elapsed=0.0
                )

        except subprocess.TimeoutExpired:
            if p:
                try:
                    if os.name == "posix":
                        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                    else:
                        p.kill()
                    p.wait(timeout=1.0)
                except Exception:
                    pass
            return ExecutionResult(
                status="timeout",
                output=f"【超时未能及时回复（已超过 {timeout} 秒限制）】",
                raw_output=f"Subprocess timed out after {timeout} seconds",
                elapsed=0.0,
                error_msg=f"TimeoutExpired ({timeout}s)"
            )
        except Exception as ex:
            return ExecutionResult(
                status="error",
                output=f"【执行异常: {str(ex)}】",
                raw_output=str(ex),
                elapsed=0.0,
                error_msg=str(ex)
            )
        finally:
            _unregister_process(session_id, p)
