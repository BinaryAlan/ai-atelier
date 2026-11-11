import os
import re
import signal
import subprocess
import time
import shlex
import threading
from typing import Dict, Any, Tuple, Optional, List
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
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if os.name == "posix":
                # 独立进程组，便于整组回收子进程
                popen_kwargs["preexec_fn"] = os.setsid

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
                err_text = (stderr or "").strip() or (stdout or "").strip() or f"Process exited with code {p.returncode}"
                return ExecutionResult(
                    status="error",
                    output=f"【CLI 执行报错: {err_text}】",
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
            if p:
                with _PROCESS_LOCK:
                    procs = ACTIVE_PROCESSES.get(session_id)
                    if procs and p in procs:
                        procs.remove(p)
                        if not procs:
                            ACTIVE_PROCESSES.pop(session_id, None)
