"""系统播报文案的双语选择。

界面语言存在 data/settings.json 的 ui_language（系统设置 → 通用与语言），
由前端切换并写回。后端生成「系统广播 / 主持人流程播报 / 群主标签」这类
界面文字时调用 L(zh, en)：英文界面下这些消息也跟着变英文。

注意：模型 prompt 本身不在这里翻译——它是给本地 CLI 的输入，与界面语言无关；
AI 的发言内容也永远保持模型原样输出。
"""
from .storage import load_settings


def ui_language() -> str:
    """读取当前界面语言，配置缺失/损坏时一律回退中文。"""
    try:
        raw = load_settings().get("ui_language")
    except Exception:
        return "zh"
    return "en" if str(raw or "zh").strip().lower() == "en" else "zh"


def L(zh: str, en: str = "") -> str:
    """按界面语言返回中文或英文文案；没给英文时保持中文。"""
    return en if (en and ui_language() == "en") else zh


def system_label(zh: str = "系统广播") -> str:
    """系统消息的发送者名称（历史消息里的中文标签由前端在渲染时转换）。"""
    mapping = {
        "系统广播": "System",
        "系统复位": "Service Recovery",
        "系统异常捕获": "Error Handler",
        "群主 (你)": "Owner (you)",
    }
    return L(zh, mapping.get(zh, "System"))
