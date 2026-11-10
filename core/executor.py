import subprocess
import time
import shlex
from typing import Dict, Any, Tuple

class Executor:
    @staticmethod
    def run_employee(employee: Dict[str, Any], prompt: str, timeout: int = 90) -> Tuple[str, float]:
        """
        调用员工（当前为 CLI 模式，后续可平滑扩展 API 模式）。
        返回: (回复文本, 耗时秒数)
        """
        start_time = time.time()
        emp_type = employee.get("type", "cli")

        if emp_type == "cli":
            cmd_template = employee.get("cli_cmd", 'agy -p "{prompt}"')
            raw_output = Executor._run_cli(cmd_template, prompt, timeout=timeout)
        elif emp_type == "api":
            # 预留给未来扩展 OpenAI / Anthropic / Ollama API 调用
            raw_output = Executor._run_api_placeholder(employee, prompt)
        else:
            raw_output = f"【错误: 未知的员工类型 {emp_type}】"

        elapsed = round(time.time() - start_time, 2)
        return raw_output.strip(), elapsed

    @staticmethod
    def _run_cli(cmd_template: str, prompt: str, timeout: int = 90) -> str:
        """
        智能解析 CLI 命令模板，将 {prompt} 替换为原样长字符串，
        避免 Shell 注入和多行换行被引号截断的问题。
        """
        try:
            # 拆分基础命令参数（不带 prompt）
            # 例如: codebuddy --model hy4std --tools "" -p "{prompt}"
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

            res = subprocess.run(
                final_args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True
            )
            return res.stdout.strip()
        except subprocess.TimeoutExpired:
            return "【超时未能及时回复（已超过最大限制）】"
        except subprocess.CalledProcessError as e:
            err_msg = e.stderr.strip() or e.stdout.strip()
            return f"【CLI 执行报错: {err_msg}】"
        except Exception as ex:
            return f"【执行异常: {str(ex)}】"

    @staticmethod
    def _run_api_placeholder(employee: Dict[str, Any], prompt: str) -> str:
        return "【API 模式正在开发支持中】"
