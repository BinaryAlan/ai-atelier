"""本机 AI CLI 注册表 —— 扫描范围与执行白名单共用同一份清单。

新增一个支持的大模型 CLI，只要在这里加一条 spec：
  * 扫描器会自动探测它装没装、装了就把它的模型列进下拉框
  * 执行器会自动把它加进白名单，并按 write_flag 处理写权限

只有 shutil.which(spec["name"]) 找得到的 CLI 才会被扫描，没装的不污染列表。
这里登记的都是「单一用途的 AI CLI 客户端」，严禁加入 python/bash/sh 等通用解释器
（见 core.executor.validate_cli_template 的安全说明）。
"""

from typing import Any, Dict, List, Optional

# 推理程度统一低 / 中 / 高，默认高
REASONING_IDS = ("low", "medium", "high")
REASONING_LABELS = {"low": "低", "medium": "中", "high": "高"}
DEFAULT_REASONING = "high"

# reasoning 的落法：
#   "agy_variant" 枚举出 -low/-medium/-high 变体时用真实变体 ID，否则 --effort
#   "effort"      --effort <level>
#   "codex"       -c model_reasoning_effort=<level>
#   "variant"     --model <id>#<level>（opencode）
#   "none"        该 CLI 没有推理档位参数，只给「默认」单档
CLI_SPECS: List[Dict[str, Any]] = [
    {
        "name": "agy", "label": "Antigravity",
        "probe": ["agy", "models"], "parser": "agy_table",
        "reasoning": "agy_variant",
        "write_flag": "--dangerously-skip-permissions", "env": [],
    },
    {
        "name": "codebuddy", "label": "CodeBuddy",
        "probe": ["codebuddy", "--help"], "parser": "codebuddy_help",
        "reasoning": "effort",
        "write_flag": "-y",
        "env": ["CODEBUDDY_API_KEY", "CODEBUDDY_BASE_URL", "CODEBUDDY_CONFIG_DIR"],
    },
    {
        "name": "opencode", "label": "OpenCode",
        "probe": ["opencode", "models"], "parser": "opencode_lines",
        # 只要 opencode 自家的 provider；github-copilot / gitlab 属于第三方接入，不要混进来
        "providers": ["opencode", "opencode-go"],
        "reasoning": "variant",
        "write_flag": "--auto",
        "env": ["OPENCODE_API_KEY", "OPENCODE_CONFIG", "GITHUB_TOKEN"],
    },
    {
        "name": "claude", "label": "Claude Code",
        "probe": ["claude", "--help"], "parser": "claude_help",
        "fallback_models": ["sonnet", "opus", "haiku"],
        "reasoning": "none",
        "write_flag": "--dangerously-skip-permissions",
        "env": ["ANTHROPIC_API_KEY", "CLAUDE_API_KEY"],
    },
    {
        "name": "codex", "label": "Codex",
        "probe": ["codex", "--help"], "parser": "codex_config",
        "fallback_models": ["gpt-5", "gpt-5-codex", "o3", "o4-mini", "gpt-4.1"],
        "reasoning": "codex",
        "write_flag": "--dangerously-bypass-approvals-and-sandbox",
        "env": ["OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_HOME"],
    },
    {
        "name": "cursor-agent", "label": "Cursor Agent",
        "probe": ["cursor-agent", "--help"], "parser": "cursor_help",
        "fallback_models": ["gpt-5", "sonnet-4", "sonnet-4-thinking"],
        "reasoning": "none",
        "write_flag": "-f",
        "env": ["CURSOR_API_KEY"],
    },
    # 以下是常见但本机可能没装的大模型 CLI：装上了就自动纳入扫描
    {
        "name": "gemini", "label": "Gemini CLI",
        "probe": ["gemini", "--help"], "parser": "help_hint",
        "fallback_models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"],
        "reasoning": "none",
        "write_flag": "--yolo", "env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
    },
    {
        "name": "qwen", "label": "Qwen Code",
        "probe": ["qwen", "--help"], "parser": "help_hint",
        "fallback_models": ["qwen3-coder-plus", "qwen-plus", "qwen-max"],
        "reasoning": "none",
        "write_flag": "--yolo", "env": ["DASHSCOPE_API_KEY", "QWEN_API_KEY"],
    },
    {
        "name": "ollama", "label": "Ollama",
        "probe": ["ollama", "list"], "parser": "ollama_table",
        "fallback_models": [],
        "reasoning": "none", "write_flag": "", "env": ["OLLAMA_HOST"],
    },
    {
        "name": "copilot", "label": "GitHub Copilot CLI",
        "probe": ["copilot", "--help"], "parser": "help_hint",
        "fallback_models": ["gpt-5", "claude-sonnet-4.5", "gemini-2.5-pro"],
        "reasoning": "none",
        "write_flag": "--allow-all-tools", "env": ["GITHUB_TOKEN", "GH_TOKEN"],
    },
]

SPEC_BY_NAME = {s["name"]: s for s in CLI_SPECS}
KNOWN_CLI_NAMES = tuple(s["name"] for s in CLI_SPECS)
# 写权限自动放行开关：只有真正有对应开关的 CLI 才登记
WRITE_FLAGS: Dict[str, List[str]] = {
    s["name"]: [s["write_flag"]]
    for s in CLI_SPECS if s.get("write_flag")
}
# 需要透传给子进程的凭据 / 服务地址（build_cli_env 的白名单来源之一）
CLI_ENV_KEYS = tuple(sorted({k for s in CLI_SPECS for k in s.get("env", [])}))


def build_cli_cmd(
    cli: str, model_id: str, level: str, variant_id: Optional[str] = None
) -> str:
    """某个 CLI + 模型 + 推理档位对应的完整执行模板（必须保留 {prompt}）。"""
    if cli == "agy":
        if variant_id:
            return f'agy --model {variant_id} -p "{{prompt}}"'
        return f'agy --model {model_id} --effort {level} -p "{{prompt}}"'
    if cli == "codebuddy":
        return (
            f'codebuddy --model {model_id} --tools "Read,Glob,Grep" '
            f'--effort {level} -p "{{prompt}}"'
        )
    if cli == "opencode":
        suffix = f"#{level}" if level in REASONING_IDS else ""
        return f'opencode run --model {model_id}{suffix} "{{prompt}}"'
    if cli == "claude":
        return f'claude -p "{{prompt}}" --model {model_id} --output-format text'
    if cli == "codex":
        effort = f" -c model_reasoning_effort={level}" if level in REASONING_IDS else ""
        return f'codex exec --skip-git-repo-check -m {model_id}{effort} "{{prompt}}"'
    if cli == "cursor-agent":
        return f'cursor-agent -p --output-format text -m {model_id} "{{prompt}}"'
    if cli == "ollama":
        return f'ollama run {model_id} "{{prompt}}"'
    # 通用写法：其余已登记 CLI 多为 Claude Code 风格的 -p print 模式
    return f'{cli} -p "{{prompt}}" --model {model_id}'
