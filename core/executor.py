import os
import signal
import subprocess
import time
import shlex
from typing import Dict, Any, Tuple

ACTIVE_PROCESSES: Dict[str, subprocess.Popen] = {}

class Executor:
    @staticmethod
    def kill_session_process(session_id: str) -> bool:
        """
        强制终止当前会话正在运行的 CLI 进程组，实现毫秒级中断。
        """
        p = ACTIVE_PROCESSES.get(session_id)
        if p and p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                time.sleep(0.1)
                if p.poll() is None:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception as e:
                print(f"终止进程异常 (PID: {p.pid}):", e)
            finally:
                ACTIVE_PROCESSES.pop(session_id, None)
                return True
        return False

    @staticmethod
    def run_employee(employee: Dict[str, Any], prompt: str, session_id: str = "default", timeout: int = 180) -> Tuple[str, float]:
        """
        调用员工（当前为 CLI 模式，后续可平滑扩展 API 模式）。
        默认超时提升至 180 秒，防止深度推理模型（如 DeepSeek）在大长文审查时被误杀。
        """
        start_time = time.time()
        emp_type = employee.get("type", "cli")
        emp_timeout = employee.get("timeout", timeout)

        if emp_type == "cli":
            cmd_template = employee.get("cli_cmd", 'agy -p "{prompt}"')
            raw_output = Executor._run_cli(cmd_template, prompt, session_id=session_id, timeout=emp_timeout)
        elif emp_type == "api":
            raw_output = Executor._run_api_placeholder(employee, prompt)
        else:
            raw_output = f"【错误: 未知的员工类型 {emp_type}】"

        elapsed = round(time.time() - start_time, 2)
        return raw_output.strip(), elapsed

    @staticmethod
    def _run_cli(cmd_template: str, prompt: str, session_id: str = "default", timeout: int = 180) -> str:
        """
        智能解析 CLI 命令模板，使用独立进程组运行，支持随时被 kill。
        """
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

        p = None
        try:
            p = subprocess.Popen(
                final_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                preexec_fn=os.setsid
            )
            ACTIVE_PROCESSES[session_id] = p

            stdout, stderr = p.communicate(timeout=timeout)

            if p.returncode in [-signal.SIGTERM, -signal.SIGKILL, 15, 9]:
                return "【任务已被用户手动暂停中断】"

            if p.returncode != 0:
                err_msg = stderr.strip() or stdout.strip()
                return f"【CLI 执行报错: {err_msg}】"

            return stdout.strip()
        except subprocess.TimeoutExpired:
            if p:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except Exception:
                    pass
            return f"【超时未能及时回复（已超过 {timeout} 秒限制）】"
        except Exception as ex:
            return f"【执行异常: {str(ex)}】"
        finally:
            ACTIVE_PROCESSES.pop(session_id, None)

    @staticmethod
    def _run_api_placeholder(employee: Dict[str, Any], prompt: str) -> str:
        return "【API 模式正在开发支持中】"
