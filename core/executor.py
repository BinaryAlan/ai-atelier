import os
import signal
import subprocess
import time
import shlex
import threading
from typing import Dict, Any, Tuple, Optional, List
from dataclasses import dataclass

ALLOWED_BINARIES = {"agy", "codebuddy", "python", "python3"}

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
    1. 必须以白名单命令开头 (agy, codebuddy, python3 等)
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
                try:
                    # 终止进程组中的所有子进程
                    pgid = os.getpgid(p.pid)
                    os.killpg(pgid, signal.SIGTERM)
                    try:
                        p.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        os.killpg(pgid, signal.SIGKILL)
                        p.wait(timeout=0.5)
                    killed_any = True
                except ProcessLookupError:
                    pass
                except Exception as e:
                    print(f"终止进程组异常 (PID: {p.pid}):", e)
        return killed_any

    @staticmethod
    def run_employee(
        employee: Dict[str, Any],
        prompt: str,
        session_id: str = "default",
        workspace_dir: Optional[str] = None,
        timeout: int = 180
    ) -> ExecutionResult:
        """
        调用参会员工/模型，返回结构化 ExecutionResult。
        """
        start_time = time.time()
        emp_type = employee.get("type", "cli")
        emp_timeout = employee.get("timeout", timeout)

        if emp_type == "cli":
            cmd_template = employee.get("cli_cmd", 'agy -p "{prompt}"')
            result = Executor._run_cli(
                cmd_template=cmd_template,
                prompt=prompt,
                session_id=session_id,
                workspace_dir=workspace_dir,
                timeout=emp_timeout
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
        return result

    @staticmethod
    def _run_cli(
        cmd_template: str,
        prompt: str,
        session_id: str = "default",
        workspace_dir: Optional[str] = None,
        timeout: int = 180
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

        # 校验工作目录
        cwd = None
        if workspace_dir and os.path.isdir(workspace_dir):
            cwd = os.path.abspath(workspace_dir)
        else:
            cwd = os.getcwd()

        p = None
        try:
            p = subprocess.Popen(
                final_args,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                preexec_fn=os.setsid
            )
            with _PROCESS_LOCK:
                if session_id not in ACTIVE_PROCESSES:
                    ACTIVE_PROCESSES[session_id] = []
                ACTIVE_PROCESSES[session_id].append(p)

            stdout, stderr = p.communicate(timeout=timeout)

            # 检查是否是被用户手动 SIGTERM / SIGKILL 中断
            if p.returncode in [-signal.SIGTERM, -signal.SIGKILL, 15, 9]:
                return ExecutionResult(
                    status="interrupted",
                    output="【任务已被用户手动暂停中断】",
                    raw_output=stderr.strip() or stdout.strip(),
                    elapsed=0.0,
                    error_msg="Process interrupted by user pause"
                )

            if p.returncode != 0:
                err_text = stderr.strip() or stdout.strip() or f"Process exited with code {p.returncode}"
                return ExecutionResult(
                    status="error",
                    output=f"【CLI 执行报错: {err_text}】",
                    raw_output=f"Exit code: {p.returncode}\n{err_text}",
                    elapsed=0.0,
                    error_msg=err_text
                )

            return ExecutionResult(
                status="success",
                output=stdout.strip(),
                raw_output=stdout,
                elapsed=0.0
            )

        except subprocess.TimeoutExpired:
            if p:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                    p.wait(timeout=0.5)
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
                    if session_id in ACTIVE_PROCESSES and p in ACTIVE_PROCESSES[session_id]:
                        ACTIVE_PROCESSES[session_id].remove(p)
                        if not ACTIVE_PROCESSES[session_id]:
                            ACTIVE_PROCESSES.pop(session_id, None)
