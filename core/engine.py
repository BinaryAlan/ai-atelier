import re
import threading
import time
import uuid
from typing import Dict, Any, Optional, Tuple, List
from core.executor import Executor, ExecutionResult, sanitize_model_output
from core.storage import load_settings, save_session, get_session, is_valid_session_id

def generate_msg_id() -> str:
    """生成防碰撞的唯一消息 ID"""
    return f"msg_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


# ------------------------- 结论解析（严格首行协议） -------------------------
# 判定只认模型输出首行的显式标记，绝不用中文子串推断通过，
# 否则「不通过 / 无法通过 / 未通过」会被判成 PASS，把隐患直接定稿。

_LEADING_NOISE = re.compile(r"^[\s\*_`#>~\-•·\[\]【】()（）<>,，.。!！?？\"'“”‘’]+")
_VERDICT_LABEL = re.compile(
    r"^(结论|判定|审查结论|审阅结论|审查结果|审阅结果|评审结论|评审意见|审阅意见|审查意见|验收结论|验收结果|验收意见|意见|结果)\s*[:：]?\s*",
    re.IGNORECASE
)


def _first_line(text: str) -> str:
    for line in (text or "").strip().splitlines():
        s = line.strip()
        if s and not s.startswith("```"):
            return s
    return ""


def _verdict_head(line: str) -> str:
    head = _LEADING_NOISE.sub("", line)
    head = _VERDICT_LABEL.sub("", head)
    head = _LEADING_NOISE.sub("", head)
    return head.upper()


def parse_review_verdict(output: str) -> str:
    """阶段二审阅结论：仅返回 PASS / BLOCKER / COMMENT。"""
    for line in (output or "").strip().splitlines()[:5]:
        s = line.strip()
        if not s or s.startswith("```"):
            continue
        head = _verdict_head(s)
        if head.startswith("BLOCKER"):
            return "BLOCKER"
        if head.startswith("PASS"):
            return "PASS"
        if _VERDICT_LABEL.match(_LEADING_NOISE.sub("", s)):
            continue
        break
    return "COMMENT"


def parse_accept_verdict(output: str) -> str:
    """闭环验收结论：仅返回 ACCEPT / REJECT（无法识别按 REJECT 处理，谨慎优先）。"""
    for line in (output or "").strip().splitlines()[:5]:
        s = line.strip()
        if not s or s.startswith("```"):
            continue
        head = _verdict_head(s)
        if head.startswith("ACCEPT"):
            return "ACCEPT"
        if head.startswith("REJECT"):
            return "REJECT"
        if _VERDICT_LABEL.match(_LEADING_NOISE.sub("", s)):
            continue
        break
    return "REJECT"


def extract_blocker_issue(output: str) -> str:
    first = _first_line(output)
    match = re.search(r"BLOCKER\s*[:：]?\s*([^\]\n】]+)", first) or re.search(r"BLOCKER\s*[:：]?\s*([^\]\n】]+)", output or "")
    if match:
        issue = match.group(1).strip(" :：-—")
        if issue:
            return issue
    return first[:200] or "发现方案严重隐患"


def latest_user_message(session: Dict[str, Any]) -> str:
    for m in reversed(session.get("messages") or []):
        if isinstance(m, dict) and m.get("sender") == "user":
            return str(m.get("content") or "")
    return ""


def latest_user_message_obj(session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """最近一条群主消息本身（不只是文本）——引用回复的信息挂在它身上。"""
    for m in reversed(session.get("messages") or []):
        if isinstance(m, dict) and m.get("sender") == "user":
            return m
    return None


def find_message(session: Dict[str, Any], msg_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not msg_id:
        return None
    for m in session.get("messages") or []:
        if isinstance(m, dict) and m.get("id") == msg_id:
            return m
    return None


def resolve_quote(session: Dict[str, Any], user_msg: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    把群主消息上的引用（quote）还原成「谁说的 + 原话」。

    只信任会话里的真实消息正文：引用被剪成摘要只用于界面展示，
    交给模型时重新按 id 取原文，避免摘要丢关键细节或被前端篡改。
    """
    quote = (user_msg or {}).get("quote")
    if not isinstance(quote, dict):
        return None
    target = find_message(session, str(quote.get("id") or ""))
    content = str((target or {}).get("content") or quote.get("excerpt") or "").strip()
    if not content:
        return None
    return {
        "id": (target or {}).get("id") or quote.get("id"),
        "sender": (target or {}).get("sender") or quote.get("sender"),
        "sender_name": (target or {}).get("sender_name") or quote.get("sender_name") or "成员",
        "content": content,
    }


def find_quote_by_question(session: Dict[str, Any], question: str) -> Optional[Dict[str, Any]]:
    """重试老消息时没有「当前用户消息」可读，就按提问原文反查当时引用了哪一条。"""
    target = str(question or "").strip()
    for m in reversed(session.get("messages") or []):
        if not isinstance(m, dict) or m.get("sender") != "user":
            continue
        if str(m.get("content") or "").strip() == target:
            resolved = resolve_quote(session, m)
            if resolved:
                return resolved
    return None


# 写进「其他成员意见」区块的结论标签：让后来者一眼分清哪些是阻断、哪些只是建议
_FEEDBACK_LABELS = {
    "PASS": "通过",
    "BLOCKER": "提出阻断",
    "COMMENT": "未给明确结论/仅建议",
    "ACCEPT": "验收通过",
    "REJECT": "验收未通过",
}


def mark_stuck(session: Dict[str, Any], employee_id: str, reason: str = "") -> None:
    """
    记下「这次是被谁卡住的」。

    只有记了人，用户点「恢复讨论」时才可能做出正确决策：第一次原地重试它，
    第二次仍停在同一人身上就自动暂缓它 —— 否则点多少次恢复都是在同一面墙上撞。
    """
    if not employee_id:
        return
    session["stuck"] = {
        "employee_id": str(employee_id),
        "reason": (reason or "未给出明确结论")[:120],
        "at": int(time.time()),
    }


def clear_stuck(session: Dict[str, Any]) -> None:
    """流程真的往前走了，之前的卡点与恢复计数一并作废。"""
    session.pop("stuck", None)
    session.pop("stuck_resume_attempts", None)


# ---------------------------------------------------------------------------
# 「跳过当前发言」请求
#
# 群主在某人正在作答时点跳过：HTTP 线程负责杀掉 CLI 进程，引擎线程在拿到
# interrupted 结果后必须知道「这不是暂停，而是要跳过这个人」。
# 用进程内登记表而不是会话字段：引擎线程手里是会话快照，写磁盘会被它的
# save_session(merge=True) 覆盖回去，反而不可靠；同一进程内内存表最准。
# ---------------------------------------------------------------------------
_SKIP_REQUESTS: Dict[str, float] = {}
_SKIP_REQUESTS_LOCK = threading.Lock()
_SKIP_REQUEST_TTL = 120  # 秒：防止「点了但没进程可杀」的请求残留到下一轮被误用


def request_skip_current(session_id: str) -> None:
    """登记一次「跳过当前发言」请求（由 HTTP 接口在杀进程前调用）。"""
    with _SKIP_REQUESTS_LOCK:
        _SKIP_REQUESTS[session_id] = time.time()


def clear_skip_current(session_id: str) -> None:
    """撤销登记（没杀到进程 / 接口校验失败时调用）。"""
    with _SKIP_REQUESTS_LOCK:
        _SKIP_REQUESTS.pop(session_id, None)


def consume_skip_current(session_id: str) -> bool:
    """引擎线程消费一次跳过请求；过期的登记视为无效，避免串到下一轮。"""
    with _SKIP_REQUESTS_LOCK:
        at = _SKIP_REQUESTS.pop(session_id, None)
    if at is None:
        return False
    return (time.time() - at) <= _SKIP_REQUEST_TTL


def bump_document_revision(session: Dict[str, Any]) -> int:
    """
    文档正文被改写时调用，递增版本号。

    版本号同时充当“审阅轮次”的界线：BLOCKER 会清空 passed_employees 让所有人重审，
    而重审针对的必然是改写后的新正文，所以“同一版本”== “同一轮审阅”。
    """
    rev = int(session.get("doc_revision") or 0) + 1
    session["doc_revision"] = rev
    return rev


def _strip_verdict_marker(text: str) -> str:
    """去掉正文开头的 [PASS] / [BLOCKER: …] 标记——已由前面的中文标签表达，避免重复噪音。"""
    m = re.match(r"^\s*[\[【]\s*(PASS|BLOCKER|ACCEPT|REJECT|COMMENT)\b[^\]】]*[\]】]\s*", text, re.IGNORECASE)
    if not m:
        return text
    rest = text[m.end():].lstrip()
    rest = re.sub(r"^[\s\-:：–—]+", "", rest).strip()
    return rest or text


# ---------------------------------------------------------------------------
# 模型能力适配：不同 CLI 的参数不同（有的禁用了全部工具），prompt 必须如实交代，
# 否则模型会去“调用工具”，然后只吐一句「我先读取…」或一堆 <tool_calls> 标记就交卷。
# ---------------------------------------------------------------------------
_TOOLS_FLAG_PAT = re.compile(r"--tools\s+(\"[^\"]*\"|'[^']*'|\S+)")
# 字数不设限：早期用「300~500 字」硬约束，反而让模型为了凑字说空话、或为了卡上限
# 把关键论证砍掉。改成「按议题复杂度自行决定篇幅」，由模型自己把握精准度。
_LENGTH_POLICY = (
    "篇幅不设上下限：由你按议题复杂度自行决定——需要展开的就展开写透，"
    "一句话能讲清的就别铺陈。每一句都要有信息量，禁止空话、复述与自我重复。"
)
_RETRY_HINT = (
    "\n\n【上一次回答没有给出实质内容】你可能只描述了计划、或输出了工具调用标记。\n"
    "请立刻直接输出完整正文：不要描述你打算做什么，不要输出任何 <tool_calls> / <invoke> 标记，"
    "也不要反问，直接给结论。"
)
# 只说了“我准备去读文件/看原型”这类计划句，等于没答
_PREAMBLE_PAT = re.compile(
    r"^(好的|收到|明白|没问题)?[，,。!！\s]*"
    r"我(先|来|去|将|会|马上|现在)?"
    r"[^。！？\n]{0,40}?"
    r"(读取|读一下|查看|看看|阅读|了解|确认|检查|翻一下|打开)"
    r"[^。！？\n]{0,40}?"
    r"(文件|目录|原型|计划|资料|代码|仓库|工作区|文档|内容|材料)"
    r"[^。！？\n]{0,20}[。！!]?$"
)


def employee_has_tools(employee: Dict[str, Any]) -> bool:
    """根据 CLI 模板判断该角色是否还有工具可用（--tools "" 即全部禁用）。"""
    cmd = str(employee.get("cli_cmd") or "")
    m = _TOOLS_FLAG_PAT.search(cmd)
    if not m:
        return True     # 未显式限制 => 沿用 CLI 默认（有工具）
    val = m.group(1).strip("\"'").strip()
    return bool(val) and val.lower() not in ("none", "false", "0")


def capability_block(employee: Dict[str, Any]) -> str:
    """把「你到底能不能读文件」如实写进 prompt——这是 DS/HY 答不完的直接诱因。"""
    if employee_has_tools(employee):
        return (
            "\n【能力说明】你可以使用只读工具（Read / Glob / Grep）查看当前工作目录内的文件；\n"
            "但必须在**同一次回答里**给出完整结论，不要只描述你打算读取什么。\n"
            "单轮读取建议不超过 5 个文件，读不到的文件不要反复重试。\n"
        )
    return (
        "\n【能力说明】本次调用**没有**任何工具：不能读取文件、不能联网。\n"
        "严禁输出 <tool_calls> / <invoke> 之类的工具标记，也严禁只回复「我先去读取…」这类计划句——\n"
        "那会拿不到任何内容，本轮回答会被直接作废。请基于上文已给出的信息输出完整正文；\n"
        "确实需要某个文件时，在回答末尾单独一行写「需要群主提供：<路径>」。\n"
    )


def looks_like_incomplete(text: str) -> bool:
    """判断一次回答是不是「没答完」：空、极短、或只说了要去读文件。"""
    t = (text or "").strip()
    if not t or len(t) < 6:
        return True
    # 带协议标记的回答（[PASS]/[ACCEPT] 等）本身就可以很短，不算残缺
    if re.match(r"^\s*[\[【]\s*(PASS|BLOCKER|ACCEPT|REJECT|COMMENT)\b", t, re.IGNORECASE):
        return False
    if len(t) < 120 and _PREAMBLE_PAT.match(t):
        return True
    return False


def is_valid_speech(msg: Dict[str, Any]) -> bool:
    """一条发言能否被当作「成员的真实观点」引用：空、只剩工具标记、只说了计划都不算。"""
    content = sanitize_model_output(str((msg or {}).get("content") or ""))
    return not looks_like_incomplete(content)


def split_valid_opinions(messages) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    把参会发言拆成「有效」与「无效（该成员本轮等于没说话）」两堆。

    主持人合成纪要时若把「我先去读文件」这类空壳当成观点喂进去，模型会顺着气口
    替它补出一整段立场——实测出现过给两位没发言的成员各写 1000 字「发言摘要」的情况，
    等于伪造会议记录。所以无效发言一律不进 prompt，并单独告知主持人谁没说话。
    """
    valid: List[Dict[str, Any]] = []
    dropped: List[str] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        sender = m.get("sender")
        if not sender or sender in ("user", "system", "moderator"):
            continue
        if is_valid_speech(m):
            valid.append(m)
        else:
            name = m.get("sender_name") or m.get("sender") or "未知成员"
            if name not in dropped:
                dropped.append(name)
    return valid, dropped


def run_with_retry(
    employee: Dict[str, Any],
    prompt: str,
    session_id: str,
    workspace_dir: Optional[str] = None
) -> "ExecutionResult":
    """
    调用一次；若回答明显残缺（空 / 只有计划句 / 只剩工具标记），带上强指令重试一次。

    工具被禁用的模型经常先回一句「我先读取…」就结束本轮，直接入库会让会议带着空发言推进，
    所以这里重试一次；仍失败才升级为 error 让上层暂停并明确告知群主原因。
    """
    res = Executor.run_employee(employee, prompt, session_id=session_id, workspace_dir=workspace_dir)

    # 执行器已清洗过一次；这里再兜一层，保证「判定是否残缺」永远针对干净正文，
    # 不依赖调用方是否走了同一条清洗路径。
    if res.status == "success":
        res.output = sanitize_model_output(res.output)

    if res.status != "success" or not looks_like_incomplete(res.output):
        return res

    print(f"[Executor] {employee.get('name', employee.get('id'))} 回答疑似未完成（{len(res.output)} 字），重试一次")
    res2 = Executor.run_employee(
        employee, prompt + _RETRY_HINT, session_id=session_id, workspace_dir=workspace_dir
    )
    res2.elapsed += res.elapsed
    if res2.status == "success":
        res2.output = sanitize_model_output(res2.output)

    if res2.status in ("stopped", "timeout"):
        res2.raw_output = f"{res.raw_output}\n--- retry ---\n{res2.raw_output}"
        return res2

    if res2.status == "success" and not looks_like_incomplete(res2.output):
        res2.raw_output = f"{res.raw_output}\n--- retry ---\n{res2.raw_output}"
        return res2

    return ExecutionResult(
        status="error",
        output="【模型两次都未给出实质回答：可能只描述了计划，或把工具调用当成了正文输出】\n"
               "请检查该角色的 CLI 参数——若模板里写了 --tools \"\"，它就没有读文件能力，"
               "需要改成 --tools \"Read,Glob,Grep\"，或把资料直接贴进议题里。",
        raw_output=f"{res.raw_output}\n--- retry ---\n{res2.raw_output}",
        elapsed=res2.elapsed,
        error_msg="empty answer after retry"
    )


def format_round_feedback(
    session: Dict[str, Any],
    exclude_id: Optional[str] = None,
    phase: int = 2,
    limit_chars: int = 400
) -> str:
    """
    汇总「本轮（当前这版文档）」其他参会成员已经给出的判断。

    CLI 是一次性无状态进程，不把这段显式塞进 prompt，后来者就完全不知道前面的人说过什么，
    只能各自从零复读文档，容易重复提问、也谈不上“圆桌讨论”。
    """
    rev = int(session.get("doc_revision") or 0)
    # 历史未打版本号的会话或尚未成稿前，不匹配旧历史，避免把过往多轮所有消息全部拉入
    if rev <= 0:
        return ""

    settings = load_settings()
    mod_id = settings.get("moderator", {}).get("id", "moderator")

    # 按成员归集本轮最新一条意见（防止单人重试/追问产生多条重复记录）
    latest_by_sender: Dict[str, Dict[str, Any]] = {}
    for m in session.get("messages") or []:
        if not isinstance(m, dict) or m.get("phase") != phase:
            continue
        sender = m.get("sender")
        if not sender or sender in ("user", "system", "moderator", mod_id):
            continue
        if exclude_id and sender == exclude_id:
            continue
        if int(m.get("doc_revision") or 0) != rev:
            continue
        latest_by_sender[sender] = m

    lines = []
    for sender, m in latest_by_sender.items():
        label = _FEEDBACK_LABELS.get((m.get("verdict") or "").upper())
        content = str(m.get("content") or "").strip()
        if not label or not content:
            continue
        # 空壳发言（只剩工具标记 / 只说了“我先去读”）不能当同事意见喂给下一位
        if not is_valid_speech(m):
            continue

        body = content if len(content) <= limit_chars else content[:limit_chars] + "…（已截断）"
        lines.append(f"[{m.get('sender_name') or '成员'}｜{label}]: {_strip_verdict_marker(body)}")

    return "\n".join(lines)


def _feedback_block(session: Dict[str, Any], exclude_id: Optional[str] = None, is_review: bool = True) -> str:
    """渲染成可直接嵌入 prompt 的区块；本轮没人发过言时给出明确交代。"""
    text = format_round_feedback(session, exclude_id=exclude_id)
    if not text:
        if not is_review:
            return ""
        return (
            "\n【本轮其他参会成员已给出的意见】:\n"
            "（本轮尚无其他成员发表意见，你是第一位审阅人）\n"
        )
    return (
        "\n【本轮其他参会成员已给出的意见（同一版文档）】:\n"
        f"{text}\n"
        "以上为同轮同事的判断，供你参考但不得盲从：已被修订解决的问题不必重复提出；\n"
        "若你认为其中某条判断有误，请直接指出并说明理由；你仍需独立完成自己的审阅结论。\n"
    )


# ---------------------------------------------------------------------------
# 群主 @ 某位成员时的定向应答 prompt
#
# CLI 是一次性无状态进程：只把「你的提问 + 人设」丢过去，它根本不知道本场会议
# 讨论的是什么、上一轮已经定了哪些结构、右侧纪要里「E01」指的是哪一条。
# 早期的定向应答 prompt 只有 200 字左右，模型只能反问或凭空编一套东西出来。
# 因此这里统一组装四块上下文：
#   1) 会议背景：议题、所处阶段、参会成员；
#   2) 聊天窗口最近的原话（群主指令 + 各成员/主持人的回应，按时间顺序）；
#   3) 右侧面板当前最新的方案纪要正文；
#   4) 群主这一次的具体问题。
# ---------------------------------------------------------------------------
_DIRECT_CONTEXT_MAX_MESSAGES = 40
_DIRECT_CONTEXT_MSG_CHARS = 800        # 单条消息最多展示多少字
_DIRECT_CONTEXT_TOTAL_CHARS = 8000     # 聊天上下文总预算
_DIRECT_DOC_MAX_CHARS = 16000          # 纪要正文上限（超长时掐头留尾）
# 失败占位文案（超时/报错）不携带任何信息，混进上下文只会污染判断
_FAILURE_NOTE_PREFIXES = ("【超时未能及时回复", "【执行出错", "【执行失败", "【进程")


def _clip_text(text: str, limit: int) -> str:
    """按字符数截断并明确标注省略量——让模型知道「这里被截过」，别把残缺当全文。"""
    t = str(text or "").strip()
    if limit <= 0 or len(t) <= limit:
        return t
    return t[:limit].rstrip() + f"…（此处省略 {len(t) - limit} 字）"


def _is_failure_note(msg: Dict[str, Any]) -> bool:
    """这条消息是不是「调用失败」占位（超时/报错），而不是真实发言。"""
    content = str((msg or {}).get("content") or "").strip()
    if not content:
        return True
    if any(content.startswith(p) for p in _FAILURE_NOTE_PREFIXES):
        return True
    # 报错/超时且内容极短，基本就是占位文案
    return (msg or {}).get("verdict") in ("TIMEOUT", "ERROR") and len(content) <= 120


def _speaker_label(msg: Dict[str, Any]) -> str:
    sender = msg.get("sender")
    if sender == "user":
        return "群主"
    if sender == "system":
        return "系统广播"
    return str(msg.get("sender_name") or sender or "成员")


def build_chat_transcript(
    session: Dict[str, Any],
    exclude_ids: Optional[set] = None,
    max_messages: int = _DIRECT_CONTEXT_MAX_MESSAGES,
    per_msg_chars: int = _DIRECT_CONTEXT_MSG_CHARS,
    total_chars: int = _DIRECT_CONTEXT_TOTAL_CHARS,
) -> str:
    """把聊天窗口最近的对话压成一段可读的文本记录（时间顺序，较早的在上）。"""
    exclude_ids = exclude_ids or set()
    msgs = [m for m in (session.get("messages") or []) if isinstance(m, dict)]

    lines: List[str] = []
    used = 0
    omitted = 0
    for m in reversed(msgs):
        if m.get("id") in exclude_ids:
            continue
        if _is_failure_note(m):
            continue
        content = " ".join(str(m.get("content") or "").split())
        if not content:
            continue
        line = f"[{_speaker_label(m)}]: {_clip_text(content, per_msg_chars)}"
        if len(lines) >= max_messages or used + len(line) > total_chars:
            omitted += 1
            continue
        lines.append(line)
        used += len(line)

    lines.reverse()
    if omitted:
        lines.insert(0, f"（更早还有 {omitted} 条消息未列出，如需可让群主补充）")
    return "\n".join(lines)


def build_document_block(session: Dict[str, Any]) -> str:
    """渲染右侧面板当前的方案纪要与正文；没有成稿时如实说明。"""
    doc = str(session.get("document") or "").strip()
    if not doc or ("等待各参会成员" in doc and len(doc) < 200):
        return "（本场会议尚未形成正式方案纪要：阶段一还在进行中，请只依据下面的聊天上下文作答。）"

    if len(doc) > _DIRECT_DOC_MAX_CHARS:
        head_len = int(_DIRECT_DOC_MAX_CHARS * 0.65)
        tail_len = int(_DIRECT_DOC_MAX_CHARS * 0.3)
        dropped = len(doc) - head_len - tail_len
        doc = (
            f"{doc[:head_len].rstrip()}\n\n"
            f"…（中间省略 {dropped} 字，完整正文以右侧面板为准）…\n\n"
            f"{doc[-tail_len:].lstrip()}"
        )
    return f"----- 方案正文开始 -----\n{doc}\n----- 方案正文结束 -----"


def session_topic(session: Dict[str, Any]) -> str:
    """会议议题：取群主的第一条消息（通常就是任务说明），退回会话标题。"""
    for m in session.get("messages") or []:
        if isinstance(m, dict) and m.get("sender") == "user":
            content = " ".join(str(m.get("content") or "").split())
            if content:
                return _clip_text(content, 600)
    return str(session.get("title") or "（未命名议题）")


def build_direct_prompt(
    session: Dict[str, Any],
    target_emp: Dict[str, Any],
    question: str,
    workspace_dir: str = "",
    exclude_msg_id: Optional[str] = None,
    is_retry: bool = False,
    quoted: Optional[Dict[str, Any]] = None,
) -> str:
    """
    群主 @ 某位成员时的完整 prompt：会议背景 + 聊天上下文 + 方案纪要 + 本条提问。

    exclude_msg_id：把「这次要重答的那条失败消息」排除掉，避免模型把自己的
    报错/超时占位当成同事发言来读。
    quoted：群主通过「💬 回复」引用某条发言时，把被引用的原话单独摆出来，
            让它明确「现在是在哪一段话的基础上追问」。
    """
    settings = load_settings()
    moderator = MeetingEngine.get_moderator(settings)
    emp_name = target_emp.get("name") or target_emp.get("id") or "成员"
    is_moderator = target_emp.get("id") == moderator.get("id")

    phase = int(session.get("phase") or 1)
    if phase <= 1:
        phase_line = "阶段一：各成员自由立论（尚未形成正式方案纪要）"
    else:
        phase_line = "阶段二：方案审阅与隐患质询（已有正式纪要与方案正文，见下方正文块）"

    attendee_names: List[str] = [f"{moderator.get('name')}(主持人)"]
    for eid in session.get("employee_ids") or []:
        if not isinstance(eid, str):
            continue
        emp = MeetingEngine.get_employee(settings, eid)
        name = emp.get("name") or eid
        if name not in attendee_names:
            attendee_names.append(name)

    persona = str(target_emp.get("system_prompt") or "").strip() or "（该角色未配置人设，按你本身的能力作答）"
    transcript = build_chat_transcript(
        session,
        exclude_ids={exclude_msg_id} if exclude_msg_id else None,
    ) or "（聊天窗口里还没有可参考的历史消息，这是本场会议的第一轮交流）"

    doc_block = build_document_block(session)
    role_line = (
        "你是本场会议的主持人，这条提问是群主单独找你的：直接作答即可，不需要推进会议流程。"
        if is_moderator else
        "你是本场会议的参会成员（不是主持人）：这条提问是群主单独问你的，只回答它，不需要推进会议流程，也不需要替别人发言。"
    )
    retry_note = (
        "\n（说明：这是群主对同一条提问的重试。上一次调用因超时/报错没有取到结果，"
        "请从头完整作答，不要提及重试本身。）"
        if is_retry else ""
    )
    quote_block = ""
    if quoted and quoted.get("content"):
        quote_author = quoted.get("sender_name") or "成员"
        quote_body = _clip_text(str(quoted.get("content")), 3000)
        quote_block = (
            "\n【群主正在回复的这条消息（引用）】\n"
            f"[{quote_author}] 的原话：\n"
            f"「{quote_body}」\n"
            "（群主是在上面这条发言的基础上追问，请紧扣它作答；如果不认同其中的说法，直接指出并说明理由。）\n"
        )

    return f"""【本场会议背景】
- 会议议题: {session_topic(session)}
- 当前阶段: {phase_line}
- 参会成员: {'、'.join(attendee_names)}
- 当前工作目录: {workspace_dir or session.get('workspace_dir') or '(未设置)'}

【你的身份】
你是【{emp_name}】。
人设定位: {persona}
{role_line}

【聊天窗口最近上下文（按时间顺序，较早的在上）】
{transcript}

【当前会议方案纪要（右侧面板最新正文）】
{doc_block}
（以上是本场会议已经确认的正式内容。回答里涉及方案结构、编号、结论时，一律以它为准，不要另立一套。）
{quote_block}
【群主这一次的具体提问】
{question}
（群主用 "@{emp_name}" 点名，这只是叫你，不必在回答里复述称呼。）{retry_note}

【回答要求】
1. 直接输出正文回答上面这一个问题，不要反问、不要复述会议流程、不要只描述你打算怎么做；
2. 上文没有的信息不要编造：确实需要工作目录里的具体文件内容时，按下方【能力说明】去读，读不到就明确说明「缺哪份材料」；
3. 引用纪要或聊天里的原话时保持原意，不要替任何成员补全未表达的观点；
4. {_LENGTH_POLICY}
{capability_block(target_emp)}"""


class MeetingEngine:
    @staticmethod
    def get_moderator(settings: Dict[str, Any]) -> Dict[str, Any]:
        mod = settings.get("moderator")
        if isinstance(mod, dict) and mod.get("id"):
            return mod
        return {
            "id": "moderator",
            "name": "Antigravity",
            "avatar": "👑",
            "avatar_color": "#d97706",
            "type": "cli",
            "cli_cmd": 'agy -p "{prompt}"',
            "system_prompt": "你是会议主持人。负责中立客观地主持会议进程、梳理记录各方发言要点、综合整理方案纪要，并在全员达成共识时出具定稿报告。",
            "timeout": 180
        }

    @staticmethod
    def get_employee(settings: Dict[str, Any], emp_id: str) -> Dict[str, Any]:
        """
        根据 ID 获取员工配置。若员工被删除或 ID 历史变更，返回安全兜底对象，绝不抛出异常。
        """
        moderator = MeetingEngine.get_moderator(settings)
        if emp_id in ["moderator", moderator.get("id"), "antigravity_moderator"]:
            return moderator

        for e in settings.get("employees", []):
            if e.get("id") == emp_id:
                return e

        # 历史别名兼容
        if emp_id == "antigravity":
            for e in settings.get("employees", []):
                if e.get("id") == "emp_agy":
                    return e
            return moderator

        # 兜底对象（防止历史会话因角色删除而崩溃）
        return {
            "id": emp_id,
            "name": emp_id,
            "avatar": "🤖",
            "avatar_color": "#64748b",
            "type": "cli",
            "cli_cmd": "",
            "system_prompt": "",
            "enabled": False,
            "timeout": 180
        }

    @staticmethod
    def detect_mention(content: str, settings: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        if "@所有人" in content or "@all" in content.lower():
            return True, None

        mod = MeetingEngine.get_moderator(settings)
        if f"@{mod['name']}" in content or "@主持人" in content:
            return False, mod["id"]

        for emp in settings.get("employees", []):
            if f"@{emp['name']}" in content or f"@{emp['id']}" in content:
                return False, emp["id"]

        return False, None

    @staticmethod
    def user_inject(session: Dict[str, Any], content: str, reply_to: Optional[str] = None):
        """
        记录群主发言，并决定这条消息该由谁应答。

        reply_to：群主点了某条发言的「💬 回复」。它等价于 @ 那个人 ——
        被引用的原话会以 quote 形式挂在消息上（界面显示引用条，prompt 里单独给出原文），
        且**优先于文本里的 @**：既然点了回复谁，就是想让谁答。
        """
        now = int(time.time())
        settings = load_settings()
        moderator = MeetingEngine.get_moderator(settings)

        # 只认会话里真实存在、且确实由成员/主持人发出的消息；
        # 不允许引用自己或系统广播（那没有可回答的对象）。
        quote = None
        quoted_msg = find_message(session, str(reply_to or "").strip() or None)
        if quoted_msg and quoted_msg.get("sender") not in (None, "", "user", "system"):
            quote = {
                "id": quoted_msg.get("id"),
                "sender": quoted_msg.get("sender"),
                "sender_name": quoted_msg.get("sender_name") or quoted_msg.get("sender"),
                "excerpt": _clip_text(str(quoted_msg.get("content") or ""), 200),
            }

        msg = {
            "id": generate_msg_id(),
            "sender": "user",
            "sender_name": "群主 (你)",
            "content": content,
            "phase": session.get("phase", 1),
            "verdict": "USER",
            "elapsed_time": 0,
            "created_at": now,
            "sent_prompt": content,
            "raw_response": content
        }
        if quote:
            msg["quote"] = quote
        session["messages"].append(msg)

        is_all, specific_emp_id = MeetingEngine.detect_mention(content, settings)
        if quote:
            # 引用回复 = 指定了应答人，文本里没有 @ 也照样定向
            specific_emp_id = quote["sender"]
            is_all = False

        def _reset_to_phase_one():
            session["phase"] = 1
            session["passed_employees"] = []
            session["blocker_under_review"] = None
            session["blocker_retry_count"] = 0
            session["review_comment_retries"] = {}

        if specific_emp_id:
            session["target_direct_mention"] = specific_emp_id
            session["pre_mention_status"] = session.get("status", "idle")
            session["status"] = "running"
        elif is_all:
            session["target_direct_mention"] = None
            _reset_to_phase_one()
            session["status"] = "running"
        else:
            session["target_direct_mention"] = None
            if session.get("status") in ["idle", "concluded"]:
                _reset_to_phase_one()
                session["status"] = "running"
            else:
                session["status"] = "running"

        # 调用方（app 层）已持有该会话的 session_lock，这里直接覆盖保存
        save_session(session)

    @staticmethod
    def _create_pause_message(moderator_id: str, moderator_name: str, phase: int, text: str) -> Dict[str, Any]:
        return {
            "id": generate_msg_id(),
            "sender": moderator_id,
            "sender_name": f"{moderator_name} (主持人)",
            "content": text,
            "phase": phase,
            "verdict": "PAUSE",
            "elapsed_time": 0,
            "created_at": int(time.time()),
            "sent_prompt": "",
            "raw_response": ""
        }

    @staticmethod
    def _create_concluded_message(moderator_id: str, moderator_name: str,
                                  absent_names: Optional[List[str]] = None) -> Dict[str, Any]:
        # 有人被跳过/暂缓时不能写「全员已审阅完成」——那等于凭空宣称一个不存在的共识
        absent = [n for n in (absent_names or []) if n]
        if absent:
            content = (
                f"📋 本轮审阅结束：其余成员均已通过，但 {'、'.join(absent)} 本轮未给出结论（已暂缓/跳过），"
                "所以这**不是**全员共识。需要它的意见时，在「👥 参会成员」面板点「↩️ 恢复」后再继续。"
            )
        else:
            content = "📋 本轮讨论方案全员已审阅完成，最新共识已整理在右侧面板。群主如有新想法或要调整方向，可随时输入新消息继续讨论。"
        return {
            "id": generate_msg_id(),
            "sender": moderator_id,
            "sender_name": f"{moderator_name} (主持人)",
            "content": content,
            "phase": 2,
            "verdict": "CONCLUDED",
            "elapsed_time": 0,
            "created_at": int(time.time()),
            "sent_prompt": "",
            "raw_response": ""
        }

    @staticmethod
    def _stop_if_interrupted(session: Dict[str, Any], session_id: str, res_status: str,
                             speaker_id: Optional[str] = None,
                             speaker_name: Optional[str] = None) -> Optional[Tuple[bool, str]]:
        """
        统一处理「用户打断当前发言」：返回 None 表示可以继续，否则返回统一的终止结果。

        两种打断必须区分开，否则「跳过」会被当成「暂停」把会议停住：
        * 暂停（🛑 按钮）→ 会话状态已被置为 paused，这里只收尾；
        * 跳过（⏭ 按钮）→ 会话仍是 running，只是登记的跳过请求要求把人移出发言位。
        """
        latest = get_session(session_id) or {}
        latest_status = latest.get("status")

        # 先看这次是不是「跳过当前发言」：它不影响会议状态，只换人继续
        if res_status == "interrupted" and consume_skip_current(session_id):
            session["current_speaker"] = None

            # A. 被 @ 的点名应答：只是不想等这句话了，人仍然留在会上
            if session.get("target_direct_mention"):
                session["target_direct_mention"] = None
                prev_status = session.pop("pre_mention_status", "idle")
                session["status"] = prev_status if prev_status in ("running", "paused") else "idle"
                session["messages"].append({
                    "id": generate_msg_id(),
                    "sender": "system",
                    "sender_name": "系统广播",
                    "content": (
                        f"⏭ 群主中断了 [{speaker_name or '被点名的成员'}] 的点名回答。"
                        "它没有被移出发言位，会议状态保持不变；需要答案可以再 @ 它一次。"
                    ),
                    "phase": session.get("phase", 1),
                    "verdict": "RECOVER",
                    "elapsed_time": 0,
                    "created_at": int(time.time()),
                    "sent_prompt": "",
                    "raw_response": ""
                })
                save_session(session, merge=True)
                return True, "点名回答已被群主中断"

            # B. 主持人在整理 / 修订：没法「跳过主持人」，只能停在这儿等人
            if not speaker_id:
                mod = MeetingEngine.get_moderator(load_settings())
                session["status"] = "paused"
                session["messages"].append(
                    MeetingEngine._create_pause_message(
                        mod["id"], mod["name"],
                        session.get("phase", 1),
                        "⏸️ 群主中断了主持人正在进行的整理工作，会议转为暂停。"
                        "点「▶️ 恢复讨论」会重新安排这一步；草案正文未被覆盖。"
                    )
                )
                save_session(session, merge=True)
                return False, "主持人整理过程被群主中断"

            # C. 正常轮次里的成员：移出发言位，换下一位
            emp_name = speaker_name or str(speaker_id)
            skipped = [e for e in (session.get("skipped_employees") or []) if isinstance(e, str)]
            if speaker_id not in skipped:
                skipped.append(speaker_id)
            session["skipped_employees"] = skipped

            # 它正在提的阻断没人验收了，视为撤回（否则会永远等一个不会回来的 [ACCEPT]）
            blocker = session.get("blocker_under_review")
            withdrawn = bool(blocker and blocker.get("raised_by") == speaker_id)
            if withdrawn:
                session["blocker_under_review"] = None
                session["blocker_retry_count"] = 0

            session["status"] = "running"
            session["messages"].append({
                "id": generate_msg_id(),
                "sender": "system",
                "sender_name": "系统广播",
                "content": (
                    f"⏭ 群主中断了 [{emp_name}] 正在进行的回答，并把它移出发言位，"
                    "会议直接交由余下成员继续。"
                    + ("\n（它正在提出的阻断问题视为撤回，问题原文仍留在上方可人工判断。）" if withdrawn else "")
                    + "\n需要它的意见时，在「👥 参会成员」面板点「↩️ 恢复」即可拉回来。"
                ),
                "phase": session.get("phase", 1),
                "verdict": "RECOVER",
                "elapsed_time": 0,
                "created_at": int(time.time()),
                "sent_prompt": "",
                "raw_response": ""
            })
            save_session(session, merge=True)
            return True, f"[{emp_name}] 的发言已被群主中断并跳过"

        if res_status != "interrupted" and latest_status != "paused":
            return None

        session["current_speaker"] = None
        if latest_status == "running":
            # 执行期间用户已点「恢复」：不要把它再写回 paused，交给重排的线程继续
            session["status"] = "running"
            save_session(session, merge=True)
            return False, "本次调用被中断，会议保持运行"
        session["status"] = "paused"
        save_session(session, merge=True)
        return False, "执行已被用户手动暂停中断"

    @staticmethod
    def active_workers(session: Dict[str, Any], settings: Dict[str, Any]) -> List[str]:
        """
        本场会议真正能发言的成员：去掉主持人、已停用/未配置 CLI 的角色、以及被群主跳过的成员。

        抽成公共方法是因为 app 层判断「这次恢复能不能推进」时必须用完全相同的口径，
        否则会出现「UI 以为还有别人可问、引擎却认为没人了」的分歧。
        """
        moderator = MeetingEngine.get_moderator(settings)
        skipped = {eid for eid in (session.get("skipped_employees") or []) if isinstance(eid, str)}
        workers: List[str] = []
        for eid in session.get("employee_ids") or []:
            if not isinstance(eid, str) or eid in workers:
                continue
            emp = MeetingEngine.get_employee(settings, eid)
            if emp.get("id") == moderator.get("id"):
                continue
            if not emp.get("enabled", True) or not emp.get("cli_cmd"):
                continue
            if eid in skipped:
                continue
            workers.append(eid)
        return workers

    @staticmethod
    def absent_names(session: Dict[str, Any], settings: Dict[str, Any], skipped) -> List[str]:
        """本轮被跳过/暂缓、因而没有表态的成员名字（用于结案时如实说明）。"""
        names: List[str] = []
        for eid in session.get("employee_ids") or []:
            if isinstance(eid, str) and eid in skipped:
                emp = MeetingEngine.get_employee(settings, eid)
                name = emp.get("name") or eid
                if name not in names:
                    names.append(name)
        return names

    @staticmethod
    def step(session: Dict[str, Any]) -> Tuple[bool, str]:
        session_id = session.get("id")

        if not is_valid_session_id(session_id):
            return False, "非法会话 id，终止推进"

        # 检查最新持久化状态：会话已被删除则立即终止（否则会「复活」已删会话）
        latest_on_disk = get_session(session_id)
        if latest_on_disk is None:
            return False, "会话已删除，终止推进"

        if latest_on_disk.get("status") == "paused":
            session["status"] = "paused"
            session["current_speaker"] = None
            return False, "会议已被暂停"

        if session.get("status") != "running":
            return False, f"会议处于 {session.get('status')} 状态，无需推进"

        settings = load_settings()
        moderator = MeetingEngine.get_moderator(settings)

        # 参会成员去重 + 过滤（已删除/已停用/未配置 CLI/被跳过的角色不再占用发言位）
        skipped = {eid for eid in (session.get("skipped_employees") or []) if isinstance(eid, str)}
        workers: List[str] = MeetingEngine.active_workers(session, settings)

        if not workers:
            session["status"] = "idle"
            session["current_speaker"] = None
            if skipped:
                session["messages"].append(
                    MeetingEngine._create_pause_message(
                        moderator["id"], moderator["name"], session.get("phase", 1),
                        "⏸️ 当前没有可继续发言的参会成员（已被跳过或不可用）。"
                        "流程回到空闲：可在「参会成员」面板恢复被跳过的成员，或直接发消息给出新指令。"
                    )
                )
            save_session(session, merge=True)
            return False, "没有可用的参会成员"

        workspace_dir = session.get("workspace_dir", "")

        # -------------------------------------------------------------
        # 1. 针对特定成员的点名直接应答 (定向呼叫，保留原会议状态)
        # -------------------------------------------------------------
        direct_target = session.get("target_direct_mention")
        if direct_target:
            target_emp = MeetingEngine.get_employee(settings, direct_target)
            last_user_obj = latest_user_message_obj(session)
            last_user_msg = str((last_user_obj or {}).get("content") or "")
            quoted = resolve_quote(session, last_user_obj)

            # 定向应答不能只给「问题 + 人设」：CLI 无状态，必须自带会议议题、
            # 聊天上下文与当前纪要，否则模型不知道群主在说什么、也不知道已有的结论。
            prompt = build_direct_prompt(
                session, target_emp, last_user_msg, workspace_dir=workspace_dir,
                quoted=quoted,
            )

            session["current_speaker"] = target_emp["name"]
            save_session(session, merge=True)

            res = Executor.run_employee(
                target_emp,
                prompt,
                session_id=session_id,
                workspace_dir=workspace_dir
            )

            # 检查执行过程中是否被用户手动暂停 / 跳过
            stop = MeetingEngine._stop_if_interrupted(
                session, session_id, res.status,
                speaker_id=target_emp["id"], speaker_name=target_emp["name"]
            )
            if stop:
                return stop

            session["current_speaker"] = None
            session["target_direct_mention"] = None

            # 恢复点名之前的会议状态
            prev_status = session.pop("pre_mention_status", "idle")
            session["status"] = prev_status if prev_status in ["running", "paused"] else "idle"

            if res.status == "timeout":
                verdict = "TIMEOUT"
            elif res.status == "error":
                verdict = "ERROR"
            else:
                verdict = "DIRECT"

            session["messages"].append({
                "id": generate_msg_id(),
                "sender": target_emp["id"],
                "sender_name": target_emp["name"],
                "content": res.output,
                "phase": session.get("phase", 1),
                "verdict": verdict,
                "elapsed_time": res.elapsed,
                "created_at": int(time.time()),
                # 定向应答的失败要能「重试」而不是「跳过此人」：
                # 它是群主点名要的答案，换人没有意义，只能同一个人重问。
                "direct_mention": True,
                # 单独记下「群主问的是什么」：重试时据此重建 prompt，
                # 不必从旧 prompt 里反解析，也不会被后来插入的新消息带偏。
                "direct_question": last_user_msg,
                # 引用回复的快照：重试时即使原用户消息被后续对话挤远，也能还原引用块
                "quoted": (
                    {
                        "id": quoted.get("id"),
                        "sender_name": quoted.get("sender_name"),
                        "content": _clip_text(str(quoted.get("content") or ""), 3000),
                    }
                    if quoted else None
                ),
                "sent_prompt": prompt,
                "raw_response": res.raw_output
            })
            save_session(session, merge=True)
            return True, f"[{target_emp['name']}] 已完成直接应答"

        phase = session.get("phase", 1)

        # -------------------------------------------------------------
        # 2. 阶段一：参会成员自由发表意见 (Brainstorm / Opening)
        # -------------------------------------------------------------
        if phase == 1:
            spoken_in_p1 = set()
            last_user_idx = -1
            for idx, m in enumerate(session["messages"]):
                if m["sender"] == "user":
                    last_user_idx = idx

            relevant_msgs = session["messages"][last_user_idx + 1:] if last_user_idx != -1 else session["messages"]
            for m in relevant_msgs:
                if m.get("phase") == 1 and m["sender"] in workers:
                    spoken_in_p1.add(m["sender"])

            next_emp_id = None
            for eid in workers:
                if eid not in spoken_in_p1:
                    next_emp_id = eid
                    break

            if next_emp_id:
                emp = MeetingEngine.get_employee(settings, next_emp_id)
                user_topic = session["messages"][last_user_idx]["content"] if last_user_idx != -1 else "方案研讨"

                prior_valid, prior_silent = split_valid_opinions(
                    [m for m in relevant_msgs if m.get("phase") == 1 and m.get("sender") in workers]
                )
                p1_context = [f"[{m['sender_name']}]: {m['content']}" for m in prior_valid]
                context_str = "\n".join(p1_context) if p1_context else "(你是第一个发言人)"
                if prior_silent:
                    context_str += (
                        f"\n（本轮以下成员尚未给出有效发言：{'、'.join(prior_silent)}；"
                        "请勿替他们补全观点，也不要假装看到过他们的意见）"
                    )
                capability = capability_block(emp)

                prompt = f"""【会议议题】: {user_topic}
本地工作目录: {workspace_dir}
你的身份是【{emp['name']}】。
人设定位: {emp.get('system_prompt', '')}

【其他参会人已发表的初步观点】:
{context_str}

【你的任务】:
请基于你的专业视角，直接给出你对该议题的核心见解、关键思路与落地建议。
要求：
1. 务实客观，{_LENGTH_POLICY}
2. 提出有建设性的方案要点，不讲客套话；
3. 信息不足时直接说明你的假设，不要反问、也不要把问题抛回来。
{capability}"""

                session["current_speaker"] = emp["name"]
                save_session(session, merge=True)

                res = run_with_retry(emp, prompt, session_id, workspace_dir)

                # 检查暂停 / 跳过
                stop = MeetingEngine._stop_if_interrupted(
                    session, session_id, res.status,
                    speaker_id=emp["id"], speaker_name=emp["name"]
                )
                if stop:
                    return stop

                session["current_speaker"] = None

                if res.status == "timeout":
                    verdict = "TIMEOUT"
                elif res.status == "error":
                    verdict = "ERROR"
                else:
                    verdict = "OPINION"

                session["messages"].append({
                    "id": generate_msg_id(),
                    "sender": emp["id"],
                    "sender_name": emp["name"],
                    "content": res.output,
                    "phase": 1,
                    "verdict": verdict,
                    "elapsed_time": res.elapsed,
                    "created_at": int(time.time()),
                    "sent_prompt": prompt,
                    "raw_response": res.raw_output
                })

                if res.status in ["timeout", "error"]:
                    session["status"] = "paused"
                    err_desc = "响应超时未回复" if res.status == "timeout" else "执行报错"
                    mark_stuck(session, emp["id"], err_desc)
                    session["messages"].append(
                        MeetingEngine._create_pause_message(
                            moderator["id"], moderator["name"], 1,
                            f"⚠️ 参会人 [{emp['name']}] {err_desc}。会议已自动挂起，等待群主介入处理。"
                        )
                    )
                    save_session(session, merge=True)
                    return True, f"[{emp['name']}] 阶段一立论异常，已挂起"

                clear_stuck(session)
                save_session(session, merge=True)
                return True, f"[{emp['name']}] 阶段一发言完毕"

            else:
                # 阶段一全部人发表完毕 ➡️ 主持人合成首版草案文档
                user_topic = session["messages"][last_user_idx]["content"] if last_user_idx != -1 else "方案研讨"
                p1_worker_msgs = [m for m in relevant_msgs
                                  if m.get("phase") == 1 and m.get("sender") in workers]
                valid_ops, dropped_names = split_valid_opinions(p1_worker_msgs)
                valid_senders = {m.get("sender") for m in valid_ops}
                silent_names = []
                for wid in workers:
                    if wid not in valid_senders:
                        w_emp = MeetingEngine.get_employee(settings, wid)
                        w_name = w_emp["name"] if w_emp else wid
                        if w_name not in silent_names:
                            silent_names.append(w_name)
                p1_context = [f"[{m['sender_name']}]: {m['content']}" for m in valid_ops]
                context_str = "\n".join(p1_context) if p1_context else "（本轮没有任何成员给出有效发言，下面只能依据议题本身整理）"
                gap_note = ""
                if silent_names:
                    gap_note = (
                        f"\n【数据完整性约束】本轮以下成员**没有**给出有效发言：{'、'.join(silent_names)}。\n"
                        "严禁替他们虚构、推测或“补全”任何立场、观点、数据与结论；纪要里最多写一句"
                        "「该成员本轮未发言」。编造他人观点会直接毁掉后续所有审阅的依据。\n"
                    )

                synth_prompt = f"""【会议纪要与方案草案整理任务】
议题: "{user_topic}"
工作目录: {workspace_dir}
各参会成员初步发言汇总如下：
{context_str}
{gap_note}
你是【{moderator['name']}】。
请综合各方建议与共识，输出一份标准的《方案草案与工作纪要 (初稿)》。
{capability_block(moderator)}
要求：
- 结构清晰、条理规整，涵盖核心结论、执行要点与待办细节；
- 篇幅按方案本身的复杂度自行决定，宁可写全也不要为了凑长度注水；
- 只转述上面**确实出现过**的发言，不得替任何成员编造主张；
- 你自己的推理或补充，必须显式标注为「主持人补充」，不要混进他人名下；
- 输出纯 Markdown 格式。"""

                session["current_speaker"] = f"{moderator['name']} (主持人正在整理草案)"
                save_session(session, merge=True)

                res = Executor.run_employee(moderator, synth_prompt, session_id=session_id, workspace_dir=workspace_dir)

                stop = MeetingEngine._stop_if_interrupted(session, session_id, res.status)
                if stop:
                    return stop

                session["current_speaker"] = None

                if res.status != "success" or len(res.output.strip()) < 20:
                    session["status"] = "paused"
                    mark_stuck(session, moderator["id"], f"主持人整理初稿失败 ({res.status})")
                    session["messages"].append(
                        MeetingEngine._create_pause_message(
                            moderator["id"], moderator["name"], 1,
                            f"⚠️ 主持人整理初稿方案失败 ({res.status})。草案文档保留未覆盖，会议暂停等待人工处理。"
                        )
                    )
                    save_session(session, merge=True)
                    return True, "主持人整理初稿失败"

                # 纪要里留一条可核对的说明：谁本轮没说话，别让后人以为那份文档是全员共识
                doc_text = res.output
                if silent_names:
                    doc_text = doc_text.rstrip() + (
                        "\n\n---\n\n### 数据完整性说明\n"
                        f"- 本轮以下成员未给出有效发言，纪要未采纳任何来自他们的观点："
                        f"{'、'.join(silent_names)}。\n"
                        "- 若需要他们的判断，请重新点名，或检查其 CLI 是否具备读文件能力（`--tools`）。\n"
                    )

                session["document"] = doc_text
                bump_document_revision(session)  # 首版正文 = 第 1 轮审阅
                session["phase"] = 2
                session["passed_employees"] = []
                session["blocker_retry_count"] = 0
                clear_stuck(session)

                session["messages"].append({
                    "id": generate_msg_id(),
                    "sender": moderator["id"],
                    "sender_name": f"{moderator['name']} (主持人)",
                    "content": "📋 主持人已综合整理首版方案纪要（已更新在右侧面板）。现正式进入【阶段二：审阅与隐患质询】！",
                    "phase": 2,
                    "verdict": "SYNTHESIS",
                    "elapsed_time": res.elapsed,
                    "created_at": int(time.time()),
                    "sent_prompt": synth_prompt,
                    "raw_response": res.raw_output
                })
                save_session(session, merge=True)
                return True, "主持人已合成首版方案草案，进入阶段二"

        # -------------------------------------------------------------
        # 3. 阶段二：方案审阅与重大隐患质询
        # -------------------------------------------------------------
        elif phase == 2:
            blocker = session.get("blocker_under_review")

            # 提出阻断问题的成员已被跳过：没人能验收，只能视为该阻断撤回，
            # 否则每次推进都会去找它要 [ACCEPT]，会议永远卡在同一个阻断上。
            if blocker and blocker.get("raised_by") in skipped:
                withdrawn = MeetingEngine.get_employee(settings, blocker.get("raised_by"))
                session["blocker_under_review"] = None
                session["blocker_retry_count"] = 0
                session["messages"].append(
                    MeetingEngine._create_pause_message(
                        moderator["id"], moderator["name"], 2,
                        f"⏭ 阻断问题的提出者 [{withdrawn.get('name')}] 已被跳过，该阻断视为撤回 "
                        f"（问题原文仍留在上方，可人工判断是否还成立）。流程继续询问余下成员。"
                    )
                )
                save_session(session, merge=True)
                return True, "提出阻断的成员已被跳过，阻断视为撤回"

            # 子分支 A: 存在等待验收的阻断问题
            if blocker:
                raiser_id = blocker.get("raised_by")
                raiser_emp = MeetingEngine.get_employee(settings, raiser_id)
                issue_text = blocker.get("issue", "")
                owner_note = latest_user_message(session)
                owner_block = f"\n【群主最新指令（必须遵守，优先级最高）】: {owner_note}\n" if owner_note else ""

                fix_prompt = f"""【方案修订任务】
当前方案草案如下：
{session['document']}
{_feedback_block(session, exclude_id=raiser_id, is_review=False)}{owner_block}
参会人【{raiser_emp['name']}】指出了以下重大问题：
"{issue_text}"

请对方案草案进行针对性修订以解决该问题。
请直接输出修订后的完整 Markdown 方案文档。"""

                session["current_speaker"] = f"{moderator['name']} (主持人正在修改草案)"
                save_session(session, merge=True)

                res_fix = Executor.run_employee(moderator, fix_prompt, session_id=session_id, workspace_dir=workspace_dir)

                stop = MeetingEngine._stop_if_interrupted(session, session_id, res_fix.status)
                if stop:
                    return stop

                if res_fix.status == "success" and len(res_fix.output.strip()) > 20:
                    session["document"] = res_fix.output
                    bump_document_revision(session)  # 正文改写 = 新一轮审阅，旧意见不再带入
                else:
                    session["status"] = "paused"
                    session["current_speaker"] = None
                    mark_stuck(session, moderator["id"], f"主持人修订方案失败 ({res_fix.status})")
                    session["messages"].append(
                        MeetingEngine._create_pause_message(
                            moderator["id"], moderator["name"], 2,
                            f"⚠️ 主持人修订方案草案失败 ({res_fix.status})。草案未被破坏，流程暂停等待处理。"
                        )
                    )
                    save_session(session, merge=True)
                    return True, "主持人修订方案失败，暂停流程"

                # 注意：CLI 是一次性无状态调用（--tools "" 也读不到工作区文件），
                # 「见右侧面板」对模型毫无意义，必须把修订后的完整正文塞进 prompt，
                # 否则验收人是在审一份它从未见过的文档。
                verify_prompt = f"""【方案修订闭环验收】
你刚才指出的重大问题为: "{issue_text}"
主持人已根据你的建议更新了方案草案，修订后的完整正文如下：
----- 方案正文开始 -----
{session['document']}
----- 方案正文结束 -----
{owner_block}
【封闭式验收要求】:
请仅针对修改是否解决了你指出的核心问题进行判定。
回答第一行必须严格为以下两者之一：
[ACCEPT] - 核心问题已解决，接受修改
[REJECT: 仍有问题简述] - 仍存在严重漏洞
随后阐述依据：{_LENGTH_POLICY}严禁发散引入不相关的新议题！"""

                session["current_speaker"] = f"{raiser_emp['name']} (正在验收方案修改)"
                save_session(session, merge=True)

                res_verify = run_with_retry(raiser_emp, verify_prompt, session_id, workspace_dir)

                stop = MeetingEngine._stop_if_interrupted(
                    session, session_id, res_verify.status,
                    speaker_id=raiser_emp["id"], speaker_name=raiser_emp["name"]
                )
                if stop:
                    return stop

                session["current_speaker"] = None

                if res_verify.status in ["timeout", "error"]:
                    verdict = "TIMEOUT" if res_verify.status == "timeout" else "ERROR"
                    mark_stuck(session, raiser_emp["id"], "验收回复超时或报错")
                    session["messages"].append({
                        "id": generate_msg_id(),
                        "sender": raiser_emp["id"],
                        "sender_name": raiser_emp["name"],
                        "content": res_verify.output,
                        "phase": 2,
                        "verdict": verdict,
                        "elapsed_time": res_verify.elapsed,
                        "created_at": int(time.time()),
                        "doc_revision": session.get("doc_revision", 0),
                        "sent_prompt": verify_prompt,
                        "raw_response": res_verify.raw_output
                    })
                    session["status"] = "paused"
                    session["messages"].append(
                        MeetingEngine._create_pause_message(
                            moderator["id"], moderator["name"], 2,
                            f"⚠️ 参会人 [{raiser_emp['name']}] 验收回复超时或报错。流程已自动暂停，等待群主决策。"
                        )
                    )
                    save_session(session, merge=True)
                    return True, "验收回复异常，已挂起流程"

                # 严格判定验收结果：只认首行的显式 [ACCEPT]，
                # 避免「[REJECT: 之前的 [ACCEPT] 无效]」这类文本被误判为通过
                verify_verdict = parse_accept_verdict(res_verify.output)
                is_accepted = verify_verdict == "ACCEPT"

                verdict = "ACCEPT" if is_accepted else "REJECT"

                session["messages"].append({
                    "id": generate_msg_id(),
                    "sender": raiser_emp["id"],
                    "sender_name": raiser_emp["name"],
                    "content": res_verify.output,
                    "phase": 2,
                    "verdict": verdict,
                    "elapsed_time": res_verify.elapsed,
                    "created_at": int(time.time()),
                    "doc_revision": session.get("doc_revision", 0),
                    "sent_prompt": verify_prompt,
                    "raw_response": res_verify.raw_output
                })

                if is_accepted:
                    session["blocker_under_review"] = None
                    session["passed_employees"] = [raiser_id]
                    session["blocker_retry_count"] = 0
                    clear_stuck(session)
                    save_session(session, merge=True)
                    return True, f"[{raiser_emp['name']}] 验收通过了修订方案"
                else:
                    # 循环防死锁与收敛保护：单次阻断最多允许 3 次修订尝试
                    retry_count = session.get("blocker_retry_count", 0) + 1
                    session["blocker_retry_count"] = retry_count

                    if retry_count >= 3:
                        session["status"] = "paused"
                        mark_stuck(session, moderator["id"], f"同一阻断已修订 {retry_count} 轮仍未达成一致")
                        session["messages"].append(
                            MeetingEngine._create_pause_message(
                                moderator["id"], moderator["name"], 2,
                                f"⚠️ 参会人 [{raiser_emp['name']}] 提出的问题已连续修订 {retry_count} 轮仍未达成一致。为防死锁，系统已自动暂停会议，请群主介入裁决或微调方案方向。"
                            )
                        )
                        save_session(session, merge=True)
                        return True, "达到最大自动修订轮次，暂停等待人工介入"

                    session["blocker_under_review"] = {
                        "raised_by": raiser_id,
                        "issue": f"{issue_text} (第{retry_count}轮反馈: {res_verify.output[:100]})",
                        "raw": res_verify.output
                    }
                    save_session(session, merge=True)
                    return True, f"[{raiser_emp['name']}] 仍有异议，进入第 {retry_count + 1} 轮修订"

            # 子分支 B: 无未决问题，让下一位参会人审阅
            passed = set(session.get("passed_employees", []))
            unpassed_ids = [eid for eid in workers if eid not in passed]

            if not unpassed_ids:
                session["status"] = "idle"
                clear_stuck(session)
                session["messages"].append(
                    MeetingEngine._create_concluded_message(
                        moderator["id"], moderator["name"],
                        MeetingEngine.absent_names(session, settings, skipped),
                    )
                )
                save_session(session, merge=True)
                return True, "全体审阅完成，转为空闲等待"

            reviewer_id = unpassed_ids[0]
            reviewer_emp = MeetingEngine.get_employee(settings, reviewer_id)
            owner_note = latest_user_message(session)
            owner_block = f"\n【群主最新指令（必须遵守，优先级最高）】: {owner_note}\n" if owner_note else ""

            feedback = _feedback_block(session, exclude_id=reviewer_id)
            capability = capability_block(reviewer_emp)

            review_prompt = f"""【方案客观审阅】
你是【{reviewer_emp['name']}】。
人设背景: {reviewer_emp.get('system_prompt', '')}
当前工作目录: {workspace_dir}

请仔细审阅最新版方案草案：
----- 方案正文开始 -----
{session['document']}
----- 方案正文结束 -----
{feedback}{owner_block}{capability}
【审阅原则】:
1. 保持客观理性与专业严谨。细微修辞或非原则性小建议不作为阻断项；
2. 只有发现存在严重逻辑漏洞、重大硬伤或不可行隐患，才允许标注为 BLOCKER；
3. 你的回答首行必须严格为以下二者之一，不得省略方括号标记：
   [PASS] - 方案整体可行、无严重漏洞，同意通过
   [BLOCKER: 问题简述] - 发现不可接受的严重问题
4. 随后客观阐述理由与改进建议：{_LENGTH_POLICY}"""

            session["current_speaker"] = reviewer_emp["name"]
            save_session(session, merge=True)

            res = run_with_retry(reviewer_emp, review_prompt, session_id, workspace_dir)

            stop = MeetingEngine._stop_if_interrupted(
                session, session_id, res.status,
                speaker_id=reviewer_emp["id"], speaker_name=reviewer_emp["name"]
            )
            if stop:
                return stop

            session["current_speaker"] = None

            if res.status in ["timeout", "error"]:
                verdict = "TIMEOUT" if res.status == "timeout" else "ERROR"
                mark_stuck(session, reviewer_id, "审阅超时或报错")
                session["messages"].append({
                    "id": generate_msg_id(),
                    "sender": reviewer_emp["id"],
                    "sender_name": reviewer_emp["name"],
                    "content": res.output,
                    "phase": 2,
                    "verdict": verdict,
                    "elapsed_time": res.elapsed,
                    "created_at": int(time.time()),
                    "doc_revision": session.get("doc_revision", 0),
                    "sent_prompt": review_prompt,
                    "raw_response": res.raw_output
                })

                session["status"] = "paused"
                err_desc = "审阅超时未回复" if res.status == "timeout" else "执行报错"
                session["messages"].append(
                    MeetingEngine._create_pause_message(
                        moderator["id"], moderator["name"], 2,
                        f"⚠️ 参会人 [{reviewer_emp['name']}] {err_desc}。会议已自动挂起，等待群主决策。"
                    )
                )
                save_session(session, merge=True)
                return True, f"[{reviewer_emp['name']}] 审阅超时/报错，流程已暂停"

            # 严格首行协议判定：只认 [PASS] / [BLOCKER ...]，绝不从中文措辞推断通过
            verdict = parse_review_verdict(res.output)
            retries = session.get("review_comment_retries") or {}
            if not isinstance(retries, dict):
                retries = {}

            if verdict == "BLOCKER":
                issue_desc = extract_blocker_issue(res.output)
                session["blocker_under_review"] = {
                    "raised_by": reviewer_id,
                    "issue": issue_desc,
                    "raw": res.output
                }
                session["passed_employees"] = []
                # 新的阻断问题 = 新一轮闭环，重试计数必须归零，否则沿用上一个问题的计数会提前暂停
                session["blocker_retry_count"] = 0
                retries.pop(reviewer_id, None)
            elif verdict == "PASS":
                passed.add(reviewer_id)
                session["passed_employees"] = list(passed)
                retries.pop(reviewer_id, None)
            else:
                # 既不是明确的 BLOCKER 也不是明确的 PASS（答非所问 / 只给建议）
                # 决不能放行 PASS；同一人连续两次给不出结论则暂停，避免无限循环烧 CLI
                comment_count = int(retries.get(reviewer_id, 0)) + 1
                retries[reviewer_id] = comment_count
                verdict = "COMMENT"
            session["review_comment_retries"] = retries

            session["messages"].append({
                "id": generate_msg_id(),
                "sender": reviewer_emp["id"],
                "sender_name": reviewer_emp["name"],
                "content": res.output,
                "phase": 2,
                "verdict": verdict,
                "elapsed_time": res.elapsed,
                "created_at": int(time.time()),
                "doc_revision": session.get("doc_revision", 0),
                "sent_prompt": review_prompt,
                "raw_response": res.raw_output
            })

            if verdict == "PASS" and set(workers).issubset(set(session["passed_employees"])):
                session["status"] = "idle"
                clear_stuck(session)
                session["messages"].append(
                    MeetingEngine._create_concluded_message(
                        moderator["id"], moderator["name"],
                        MeetingEngine.absent_names(session, settings, skipped),
                    )
                )
            elif verdict == "COMMENT" and int(retries.get(reviewer_id, 0)) >= 2:
                # 收敛保护：同一人连续两次给不出明确结论，暂停而不是无限重问
                session["status"] = "paused"
                mark_stuck(session, reviewer_id, "连续两次未给出 [PASS] / [BLOCKER] 结论")
                session["messages"].append(
                    MeetingEngine._create_pause_message(
                        moderator["id"], moderator["name"], 2,
                        f"⚠️ 参会人 [{reviewer_emp['name']}] 连续 {retries.get(reviewer_id)} 次未给出明确的 "
                        f"[PASS] / [BLOCKER] 结论，为避免无限循环已自动暂停，请群主介入裁决。"
                    )
                )

            save_session(session, merge=True)
            return True, f"[{reviewer_emp['name']}] 完成审阅，结论: {verdict}"

        return False, "无动作"
