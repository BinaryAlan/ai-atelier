import codecs
import json
import os
import random
import re
import signal
import subprocess
import time
import shlex
import threading
from typing import Callable, Dict, Any, Tuple, Optional, List
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


# ---------------------------------------------------------------------------
# 模型「只说了要去做、却没给结论」的开场白识别。
#
# 工具被禁用的模型（--tools ""）经常先回一句「我先读取产物再判断」就结束本轮，
# 中文、英文都会出现（审核成员常用英文，如 "I'll read the ... before judging."）。
# 这种回答不是结论，不能当成有效产出，需要重试或判为「无意见」。
# 这是 17d6e8a 在旧圆桌系统里做过的修复，任务系统重构时被连带删除，现补回。
# ---------------------------------------------------------------------------
_PREAMBLE_ZH = re.compile(
    r"^\s*(好的|收到|明白|没问题)?[，,。!！\s]*"
    r"我(先|来|去|将|会|马上|现在)?"
    r"[^。！？\n]{0,40}?"
    r"(读取|读一下|查看|看看|阅读|了解|确认|检查|翻一下|打开)"
    r"[^。！？\n]{0,80}"
)
_PREAMBLE_EN = re.compile(
    r"^(ok(ay)?|sure|got it|alright|understood)?[,.\s]*"
    r"(i'?ll|i will|i'?m going to|i am going to|let me|first,?\s*(i'?ll|let me)?)"
    r"[^.!?\n]{0,60}?"
    r"(read|review|check|look|examine|inspect|open|go through|scan|browse)"
    r"[^.!?\n]{0,60}?"
    r"(file|files|artifact|artifacts|document|documents|doc|docs?|code|repo|repository|context|workspace|plan|source)"
    r"[^.!?\n]{0,60}[.!?]?$",
    re.IGNORECASE,
)


def looks_like_incomplete(text: str) -> bool:
    """判断一次回答是不是「没答完」：空、极短，或只说了要去读文件。

    带结论标记的回答（[通过]/[意见]/[PASS]…）本身可以很短，不算残缺。
    """
    t = (text or "").strip()
    if not t or len(t) < 6:
        return True
    if re.match(
        r"^\s*[\[【]\s*(通过|意见|pass|blocker|accept|reject|comment)\b",
        t, re.IGNORECASE,
    ):
        return False
    if len(t) < 200 and (_PREAMBLE_ZH.match(t) or _PREAMBLE_EN.match(t)):
        return True
    return False


# CLI 模板里的 --tools 参数："" 表示工具全被禁用，此时模型读不到任何文件。
_TOOLS_FLAG_PAT = re.compile(r"--tools\s+(\"[^\"]*\"|'[^']*'|\S+)")


def employee_has_tools(employee: Dict[str, Any]) -> bool:
    cmd = str((employee or {}).get("cli_cmd") or "")
    m = _TOOLS_FLAG_PAT.search(cmd)
    if not m:
        return True  # 未显式限制 => 沿用 CLI 默认（有工具）
    val = m.group(1).strip("\"'").strip()
    return bool(val) and val.lower() not in ("none", "false", "0")


def capability_block(employee: Dict[str, Any]) -> str:
    """把「到底能不能读文件」如实写进 prompt——这是模型只回开场白的直接诱因。"""
    if employee_has_tools(employee):
        return (
            "\n【能力说明】你可以用只读工具（Read / Glob / Grep）查看当前工作目录内的文件；"
            "但必须在同一次回答里给出完整结论，不要只描述你打算读取什么。"
        )
    return (
        "\n【能力说明】本次调用没有任何工具：不能读取文件、不能联网。"
        "严禁只回复「我先去读取…」这类计划句，也不要输出工具调用标记——"
        "那等于没有结论，本轮会被判为无效。请直接基于上方已给出的信息给出结论；"
        "确实缺少某个文件时，在回答末尾单独一行写「需要提供：<路径>」。"
    )


# 读权限与写权限是两件事，之前只有 write_access 一个开关：
#   write_access=False 时模板里若写着 --tools ""，成员连产物都读不到，
#   审核成员于是只回一句「我先读一下再判断」就结束（见 looks_like_incomplete）。
# 现在统一按「读工具 / 写工具」两组补齐，模板里残留的空白名单会被就地修好。
_READ_TOOLS = ("Read", "Glob", "Grep")
_WRITE_TOOLS = ("Write", "Edit", "Bash")


def compose_tools_value(read_access: bool, write_access: bool) -> str:
    """按两项权限拼出 codebuddy 的 --tools 值。"""
    tools = list(_READ_TOOLS) if read_access else []
    if write_access:
        tools += [t for t in _WRITE_TOOLS if t not in tools]
    return ",".join(tools)


def normalize_cli_tools(
    cmd_template: str,
    read_access: bool = True,
    write_access: bool = False,
) -> str:
    """把 CLI 模板里的 --tools 值按权限补齐/收敛，非 codebuddy 模板原样返回。

    只在模板确实带 --tools 时动它：`default` / `all` 视为用户显式放开，保持不动。
    """
    cmd = str(cmd_template or "")
    match = _TOOLS_FLAG_PAT.search(cmd)
    if not match:
        return cmd

    raw = match.group(1)
    current = raw.strip("\"'").strip()
    if current.lower() in ("default", "all"):
        return cmd

    have = [t.strip() for t in current.split(",") if t.strip()]
    if not read_access:
        have = [t for t in have if t.lower() not in {x.lower() for x in _READ_TOOLS}]
    if not write_access:
        have = [t for t in have if t.lower() not in {x.lower() for x in _WRITE_TOOLS}]

    lower = {t.lower() for t in have}
    want = list(_READ_TOOLS) if read_access else []
    if write_access:
        want += list(_WRITE_TOOLS)
    for tool in want:
        if tool.lower() not in lower:
            have.append(tool)
            lower.add(tool.lower())

    value = ",".join(have)
    start, end = match.span(1)
    return cmd[:start] + f'"{value}"' + cmd[end:]


# ---------------------------------------------------------------------------
# 流式输出
#
# 起因：`codebuddy -p` / `agy -p` 默认把整段回答攒到最后一次性写 stdout，界面在整个
# 等待期间只能显示「正在思考中」，长任务看起来像卡死。两家 CLI 都支持
# `--output-format stream-json`：codebuddy 配 `--include-partial-messages` 给逐字增量，
# agy 按 step 推 text_delta。这里把它解析成「已生成的文本 + 当前活动（思考/输出/读文件）」，
# 交给上层暂存和展示，同时从 result 事件里取最终文本，保证落库内容与界面看到的一致。
#
# 只对白名单里的两家 CLI 注入；注入后若 stdout 不是 NDJSON（版本差异 / 用户模板另有打算），
# 自动退回纯文本模式。
# ---------------------------------------------------------------------------
_STREAM_JSON_FLAGS = ("--output-format", "stream-json", "--include-partial-messages", "--verbose")
# agy 也支持 stream-json，但没有 --include-partial-messages（它按 step 推 text_delta），
# 多给一个未知参数会把 CLI 直接打挂，所以两家分开列。
_AGY_STREAM_FLAGS = ("--output-format", "stream-json")


class StreamJsonParser:
    """解析 codebuddy `--output-format stream-json` 的事件流。

    - `live_text`：给界面看的「已经写到哪儿了」（当前这一段的思考 + 正文）
    - `final_text`：这次执行的最终输出（优先取 result 事件，退化为**最后一段**正文）
    - `phase` / `activity`：当前在思考、在输出，还是在调 Read/Grep 之类的工具

    为什么要「分段」：模型说到一半去调工具（读文件、写文件、跑命令）时，CLI 会让这一轮
    回答就此结束，换个 step 重新开口。旧实现把所有 step 的 text_delta 首尾相接，
    于是落库内容会变成「写到一半的前缀 + 另一段半截 + 重写后的完整回答」，
    聊天窗口里就是同一句话被切断、重复三遍（实测 agy 改文档时必现）。
    现在每换一个 step / 一条 assistant 消息就另起一段，对外只暴露最后一段。
    """

    def __init__(self) -> None:
        self.thinking: List[str] = []
        self.text: List[str] = []
        # 分段缓冲：[{"think": ..., "text": ...}]，最后一段才是要展示/落库的回答
        self._segments: List[Dict[str, str]] = [{"think": "", "text": ""}]
        self._seg_key: Any = None
        self._seg_pending = False
        self._msg_seq = 0
        # 完整 assistant 消息里的正文：只有模型一口气返回整段（没有 partial 事件）时用得上
        self.assistant_texts: List[str] = []
        self.result_text = ""
        self.activity = ""
        self.phase = ""
        self.saw_event = False
        self._buf = ""

    # --- 分段缓冲 ---------------------------------------------------------
    def _switch_segment(self, key: Any) -> None:
        """换到新的回答段（step_index / assistant 消息变化时调用）。"""
        if self._seg_key is None:
            self._seg_key = key
            return
        if key != self._seg_key:
            self._seg_key = key
            self._seg_pending = True

    def _touch_segment(self) -> Dict[str, str]:
        if self._seg_pending:
            self._segments.append({"think": "", "text": ""})
            self._seg_pending = False
        return self._segments[-1]

    def _append_text(self, delta: str) -> None:
        self.text.append(delta)
        self._touch_segment()["text"] += delta

    def _append_think(self, delta: str) -> None:
        self.thinking.append(delta)
        self._touch_segment()["think"] += delta

    def _last_segment(self) -> Dict[str, str]:
        """最后一段；刚换段还没写入时回退到上一段，界面不会闪成空白。"""
        for seg in reversed(self._segments):
            if (seg["think"] or seg["text"]):
                return seg
        return self._segments[-1]

    def feed(self, chunk: str) -> None:
        """喂入一段解码后的文本，按行切分 NDJSON。"""
        if not chunk:
            return
        self._buf += chunk
        # 事件以换行分隔；最后一行可能只写了一半，留在缓冲里等下一次
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._consume(line)
        # 有些版本最后一条事件后没有换行，收尾时由 finish() 兜住

    def finish(self) -> None:
        if self._buf.strip():
            self._consume(self._buf)
            self._buf = ""

    def _consume(self, line: str) -> None:
        line = (line or "").strip()
        if not line.startswith("{"):
            return
        try:
            data = json.loads(line)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        self.saw_event = True
        # 两家 CLI 的事件形状不同：codebuddy 用 type=stream_event/result，
        # agy 用 event=init/step_update/result，这里按字段是否存在分派。
        kind = data.get("type") or data.get("event")

        if kind == "step_update":
            # agy：一个 step 一条，text_delta 是这一步新增的正文。
            # step_index 变了就是「换了一口气重新说」——必须另起一段，不能接在上一段后面。
            step = data.get("step_update") or {}
            idx = step.get("step_index")
            if isinstance(idx, int):
                self._switch_segment(("step", idx))
            delta = step.get("text_delta")
            if isinstance(delta, str) and delta:
                self._append_text(delta)
                self.phase = "writing"
            step_type = str(step.get("step_type") or "")
            if step_type and step_type not in ("agent_response", "user_input") and not isinstance(delta, str):
                # 工具 / 检索一类的步骤：agy 的 step_type 只有一个笼统的 "tool"，
                # 真正有用的是 tool_name（read_file / run_command …）与参数里的路径。
                # 只把 step_type 扔给界面，用户看到的就是一个「tool」——等于没说。
                info = step.get("tool_info") if isinstance(step.get("tool_info"), dict) else {}
                params = info.get("parameters") if isinstance(info.get("parameters"), dict) else {}
                detail = ""
                for key in ("Path", "path", "file_path", "pattern", "command", "query", "url"):
                    if params.get(key):
                        detail = str(params[key])
                        break
                name = str(step.get("tool_name") or "").strip() or step_type
                self.activity = f"{name} · {detail[:80]}" if detail else name
                self.phase = "tool"
        elif kind == "stream_event":
            event = data.get("event") or {}
            etype = event.get("type")
            if etype == "message_start":
                # 新一轮 assistant 消息（工具调用之后重新开口）→ 另起一段
                self._msg_seq += 1
                self._switch_segment(("msg", self._msg_seq))
            elif etype == "content_block_start":
                block = event.get("content_block") or {}
                if block.get("type") == "tool_use":
                    self.activity = str(block.get("name") or "工具")
                    self.phase = "tool"
            elif etype == "content_block_delta":
                delta = event.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "thinking_delta":
                    self._append_think(str(delta.get("thinking") or ""))
                    self.phase = "thinking"
                elif dtype == "text_delta":
                    self._append_text(str(delta.get("text") or ""))
                    self.phase = "writing"
        elif kind == "assistant":
            content = (data.get("message") or {}).get("content") or []
            if not isinstance(content, list):
                return
            texts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    texts.append(str(block.get("text")))
                elif block.get("type") == "tool_use":
                    # 完整块里才带参数：把「读了哪个文件 / 搜了什么」显示出来，
                    # 模型闷头读文件的那几十秒也有可见进展
                    name = str(block.get("name") or "工具")
                    args = block.get("input") if isinstance(block.get("input"), dict) else {}
                    detail = (
                        args.get("file_path") or args.get("path") or args.get("pattern")
                        or args.get("command") or args.get("query") or ""
                    )
                    self.activity = f"{name} · {str(detail)[:80]}" if detail else name
                    self.phase = "tool"
            if texts:
                self.assistant_texts.append("".join(texts))
        elif kind == "result":
            payload = data.get("result")
            if isinstance(payload, dict):
                # agy：{status, response}
                self.result_text = str(payload.get("response") or payload.get("result") or "")
            else:
                self.result_text = str(payload or "")

    @property
    def live_text(self) -> str:
        seg = self._last_segment()
        head = seg["think"].strip()
        body = seg["text"]
        if head and body:
            return head + "\n\n" + body
        return head or body

    @property
    def final_text(self) -> str:
        if self.result_text.strip():
            return self.result_text
        # result 事件缺失时用最后一段：前面那些几行半截话是被工具打断的草稿，
        # 拼进来就成了「同一句话重复三遍」
        body = self._last_segment()["text"]
        if body.strip():
            return body
        if self.assistant_texts:
            return self.assistant_texts[-1]
        return ""


def stream_flags(args: List[str]) -> List[str]:
    """这份 CLI 参数要补哪些流式开关；不支持就返回空列表。

    - 只对白名单里的两家 CLI 动手，且必须是 print 模式（stream-json 只在 -p 下生效）
    - 模板自己写了 --output-format 就不覆盖（可能就是要 json / text）
    """
    if not args:
        return []
    base = os.path.basename(args[0]).lower()
    if "--output-format" in args:
        return []
    if not any(a in ("-p", "--print") for a in args):
        return []
    if base == "codebuddy":
        return list(_STREAM_JSON_FLAGS)
    if base == "agy":
        return list(_AGY_STREAM_FLAGS)
    return []


def wants_stream_json(args: List[str]) -> bool:
    """这份 CLI 参数能不能加流式开关（保留旧名字，语义等同 stream_flags 非空）。"""
    return bool(stream_flags(args))


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
# （读工具见 _READ_TOOLS：写权限不再顺带把读关掉，两者由 UI 的两个勾选框分别控制）


def apply_access_mode(
    args: List[str],
    write_access: bool,
    read_access: bool = True,
) -> List[str]:
    """
    按角色的读/写权限给 CLI 参数补上对应开关（对同一份模板可重复调用，不会叠加）。

    - write_access：加自动放行开关，并把 Write/Edit/Bash 补进 --tools 白名单
    - read_access：默认 True。只读角色也必须能 Read/Glob/Grep，否则成员读不到产物，
      只会回一句「我先读取一下」就结束本轮（模板遗留 --tools "" 是历史 bug）。
    - 两项都关：--tools 收敛成 ""，即完全不借助工具。
    """
    if not args:
        return []

    base = os.path.basename(args[0]).lower()
    out = list(args)

    if write_access:
        for flag in _WRITE_FLAGS.get(base, []):
            if flag not in out and not (flag == "-y" and "--dangerously-skip-permissions" in out):
                out.append(flag)

    if base == "codebuddy" and "--tools" in out:
        idx = out.index("--tools")
        if idx + 1 < len(out):
            current = out[idx + 1].strip()
            if current.lower() not in ("default", "all"):
                have = [t.strip() for t in current.split(",") if t.strip()]
                drop = {t.lower() for t in _WRITE_TOOLS if not write_access}
                drop |= {t.lower() for t in _READ_TOOLS if not read_access}
                have = [t for t in have if t.lower() not in drop]

                lower = {t.lower() for t in have}
                want = list(_READ_TOOLS) if read_access else []
                if write_access:
                    want += list(_WRITE_TOOLS)
                for tool in want:
                    if tool.lower() not in lower:
                        have.append(tool)
                        lower.add(tool.lower())
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
        on_chunk: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> ExecutionResult:
        """
        调用一位 AI 成员，返回结构化 ExecutionResult。

        on_chunk 传入时会边跑边回调 (累计文本, {phase, activity, chars})，
        界面据此显示「正在写什么」；不传则退回一次性的 communicate()。
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
                write_access=bool(employee.get("write_access")),
                # 老配置没有这个字段：缺省视为有读权限（历史行为不该让成员突然读不到产物）
                read_access=bool(employee.get("read_access", True)),
                on_chunk=on_chunk,
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
    def _stream_process(
        p: subprocess.Popen,
        timeout: Optional[int],
        on_chunk: Callable[[str, Dict[str, Any]], None],
        parser: Optional[StreamJsonParser] = None,
    ) -> Tuple[str, str, str]:
        """边读边回调：CLI 每写出一段就交给调用方，界面可以边生成边显示。

        两个线程分别抽干 stdout / stderr，避免任一管道写满把子进程卡死；
        回调限流到约 3 次/秒，防止界面被过细的增量刷爆。
        用 buffer.read1 而不是 read(n)：read(n) 会等凑满 n 个字符才返回，
        流式就变成了「攒够才吐一次」。

        返回 (交给上层的最终文本, stdout 原文, stderr 原文)。parser 存在时
        最终文本取自 result 事件，stdout 原文只用于排查 / 解析失败兜底。
        """
        out_parts: List[str] = []
        err_parts: List[str] = []
        last_emit = [0.0]
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        err_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        def _snapshot() -> Tuple[str, Dict[str, Any]]:
            if parser is not None:
                text = parser.live_text
                return text, {
                    "phase": parser.phase,
                    "activity": parser.activity,
                    "chars": len(text),
                }
            text = "".join(out_parts)
            return text, {"phase": "writing", "chars": len(text)}

        def _emit(force: bool = False) -> None:
            now = time.time()
            if not force and now - last_emit[0] < 0.33:
                return
            last_emit[0] = now
            text, info = _snapshot()
            try:
                on_chunk(text, info)
            except Exception:
                pass

        def _pump(stream, parts, dec, emit: bool) -> None:
            try:
                raw = getattr(stream, "buffer", stream)
                while True:
                    chunk = raw.read1(4096)
                    if not chunk:
                        break
                    piece = dec.decode(chunk)
                    parts.append(piece)
                    if parser is not None and emit:
                        parser.feed(piece)
                    if emit:
                        _emit()
            except Exception:
                pass

        t_out = threading.Thread(target=_pump, args=(p.stdout, out_parts, decoder, True), daemon=True)
        t_err = threading.Thread(target=_pump, args=(p.stderr, err_parts, err_decoder, False), daemon=True)
        t_out.start()
        t_err.start()
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                else:
                    p.kill()
            except Exception:
                pass
            raise
        finally:
            t_out.join(timeout=1.5)
            t_err.join(timeout=1.5)

        if parser is not None:
            parser.finish()

        raw_out = "".join(out_parts)
        if parser is not None and parser.saw_event:
            final_text = parser.final_text or raw_out
        else:
            final_text = raw_out
        # 收尾补一次完整文本：限流会把最后一小段吞掉，界面会停在「少一句」的状态
        try:
            text, info = _snapshot()
            on_chunk(final_text if parser is None or not parser.saw_event else text, info)
        except Exception:
            pass
        return final_text, raw_out, "".join(err_parts)

    @staticmethod
    def _run_cli(
        cmd_template: str,
        prompt: str,
        session_id: str = "default",
        workspace_dir: Optional[str] = None,
        timeout: Optional[int] = None,
        write_access: bool = False,
        read_access: bool = True,
        on_chunk: Optional[Callable[[str, Dict[str, Any]], None]] = None,
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
        final_args = apply_access_mode(final_args, write_access, read_access)

        # 需要流式时再注入输出格式：模板本身不改，解析失败时也能退回纯文本
        flags = stream_flags(final_args) if on_chunk else []
        stream_mode = bool(flags)
        if stream_mode:
            final_args = final_args + [f for f in flags if f not in final_args]

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
                # 每次尝试都换一个干净的解析器：端口冲突重试时，旧 attempt 的半截文本不能带过来
                parser = StreamJsonParser() if stream_mode else None
                p = subprocess.Popen(final_args, **popen_kwargs)
                with _PROCESS_LOCK:
                    ACTIVE_PROCESSES.setdefault(session_id, []).append(p)

                if on_chunk is None:
                    stdout, stderr = p.communicate(timeout=timeout)
                else:
                    # 流式模式下 stdout 已是「最终文本」：raw 只用于解析失败兜底与排查
                    stdout, _raw_stdout, stderr = Executor._stream_process(
                        p, timeout, on_chunk, parser
                    )

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
