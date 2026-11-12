"""AI 团队任务系统。

按 docs/task-review-protocol.md 实现：单任务 / 协助任务、每成员 FIFO 队列
（同一成员串行、不同成员并行）、协助任务三环节、消息与卡片上的取消与重试。

设计约定：
- 所有会话读写都经过 store.session_lock，避免后台线程覆盖用户写入。
- 一次模型执行 = 一个「执行单元」，用 execution_key 标识；取消时对同一个
  key 调用 killer（默认为 Executor.kill_session_process）终止进程组。
- runner / killer / store 都可注入，便于在测试里用假执行器验证并发、取消与重试。
"""

import json
import inspect
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import core.storage as storage
import core.streaming as streaming
from core.executor import (
    Executor,
    sanitize_model_output,
    looks_like_incomplete,
    capability_block,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPTS_DIR = os.path.join(BASE_DIR, "prompts")

TYPE_SINGLE = "single"
TYPE_COLLAB = "collab"

STAGE_EXEC = "exec"
STAGE_REVIEW = "review"
STAGE_DELIVER = "deliver"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DELIVERED = "delivered"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_NO_OPINION = "no_opinion"
STATUS_DONE = "done"

MEMBER_WAITING = "waiting"
MEMBER_IN_PROGRESS = "in_progress"

# 等不到成员空闲时的处理：不执行、把环节标成失败并允许重试。
# 早期实现是「超时就照跑」，那会让同一成员同时跑两个 CLI 进程、并发写同一工作目录。
MEMBER_BUSY_SUMMARY = "等待该成员空闲超时，本次未执行；可稍后重试"

# 任务终态：交付、取消、失败、超时。超时同样是「这一轮已经结束」，
# 不列入终态会让它既不能被当作结束（重启恢复、队列清理的判断依据），
# 又会和成员状态的「超时」说法不一致（设计文档 6.3）。
FINAL_TASK_STATES = (STATUS_DELIVERED, STATUS_CANCELLED, STATUS_FAILED, STATUS_TIMEOUT)
RETRYABLE_STATES = (STATUS_FAILED, STATUS_CANCELLED, STATUS_TIMEOUT, STATUS_NO_OPINION)

# 审核回答「没给结论」时的语义层重试提示：只说该直接给结论，不重复整段任务说明。
REVIEW_RETRY_HINT = (
    "\n\n【上一次回答没有给出审核结论】你可能只描述了「准备去读什么」，"
    "或只输出了工具调用标记。请立刻直接给出结论：首行写 [通过] 或 [意见]，"
    "第二行写核对范围，需要时按规程列条目；不要描述你打算做什么，也不要反问。"
)


def _accepts_on_chunk(runner: Any) -> bool:
    """执行器是否接受 on_chunk（流式回调）。拿不到签名时按不支持处理。"""
    try:
        sig = inspect.signature(runner)
    except (TypeError, ValueError):
        return False
    params = sig.parameters.values()
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
        return True
    return "on_chunk" in sig.parameters


def task_id() -> str:
    return "t_" + uuid.uuid4().hex[:8]

def msg_id() -> str:
    return "m_" + uuid.uuid4().hex[:10]


def execution_key(session_id: str, tid: str, stage: str, member_id: str) -> str:
    return f"{session_id}:{tid}:{stage}:{member_id}"


def ensure_task_fields(session: Dict[str, Any]) -> None:
    """会话缺少任务字段时补齐，保证读写不报错。"""
    if not isinstance(session.get("tasks"), dict):
        session["tasks"] = {}
    if not isinstance(session.get("task_queues"), dict):
        session["task_queues"] = {}
    if "active_task_id" not in session:
        session["active_task_id"] = None


# ---------------------------------------------------------------------------
# 指令解析
# ---------------------------------------------------------------------------

def _member_candidates(settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [e for e in (settings.get("employees") or []) if isinstance(e, dict) and e.get("id")]


def find_member(settings: Dict[str, Any], emp_id: str) -> Optional[Dict[str, Any]]:
    """按 id 找成员，不看启停状态（报错时也要能取到名字）。"""
    for emp in settings.get("employees") or []:
        if isinstance(emp, dict) and str(emp.get("id")) == str(emp_id):
            return emp
    return None


def usable_member(settings: Dict[str, Any], emp_id: str) -> Optional[Dict[str, Any]]:
    """可用于执行的成员：已启用且配了 CLI。停用或缺 CLI 一律视为不可用。"""
    emp = find_member(settings, emp_id)
    if emp and emp.get("enabled", True) and emp.get("cli_cmd"):
        return emp
    return None


def _member_label(settings: Dict[str, Any], emp_id: str) -> str:
    emp = find_member(settings, emp_id) or {}
    return str(emp.get("name") or emp_id)


def member_aliases(emp: Dict[str, Any]) -> List[str]:
    """成员可被 @ 的别名：全名、id、以及全名括号里的短名（如 DS4 / HY4）。"""
    name = str(emp.get("name") or "")
    short = re.findall(r"[（(]([^（）()]+)[）)]", name)
    return [a for a in [name, str(emp.get("id") or "")] + short if a]


def _mention_aliases(emp: Dict[str, Any]) -> List[str]:
    """可被 @ 的名字：别名 + 去掉括号的短名（如 DeepSeek-V4），长名优先。"""
    out = list(member_aliases(emp))
    head = re.split(r"[（(]", str(emp.get("name") or ""), maxsplit=1)[0].strip()
    if head and head not in out:
        out.append(head)
    out.sort(key=len, reverse=True)
    return out


@lru_cache(maxsize=512)
def _alias_pattern(alias: str) -> "re.Pattern":
    """把别名编译成宽松模式：字母数字必须按序出现，其余字符（空格、连字符、下划线、
    括号、斜杠）可有可无。

    这样 `@agy`、`@//agy/`（编辑器里那种文件引用写法）、`@DeepSeek-V4 (DS4)`、
    `@deepseek-v4/(DS4)` 都能对上同一个成员。
    """
    chunks = []
    for ch in alias:
        if ch.isalnum():
            chunks.append(re.escape(ch))
        else:
            chunks.append(r"[\s\-_.()（）/\\]*")
    return re.compile("".join(chunks), re.IGNORECASE)


def _match_mention_at(
    text: str, pos: int, settings: Dict[str, Any]
) -> Tuple[Optional[Dict[str, Any]], int]:
    """pos 指向 '@'，返回 (成员, 结束下标)；对不上就返回 (None, pos)。"""
    n = len(text)
    if pos >= n or text[pos] != "@":
        return None, pos
    start = pos + 1
    while start < n and text[start] in "/\\":
        start += 1
    best: Optional[Tuple[Dict[str, Any], int]] = None
    for emp in _member_candidates(settings):
        for alias in _mention_aliases(emp):
            if not alias:
                continue
            m = _alias_pattern(alias).match(text, start)
            if not m:
                continue
            end = m.end()
            # 后面紧跟英文字母/数字说明这是更长的一个词：@agy-pro 不是 @agy
            if end < n and text[end].isascii() and text[end].isalnum():
                continue
            # 顺手吃掉 `@//agy/` 这种写法尾巴上的斜杠，别把它留在任务内容里
            while end < n and text[end] in "/\\":
                end += 1
            if best is None or end > best[1]:
                best = (emp, end)
    if best is None:
        return None, pos
    return best


def _mention_start_ok(text: str, pos: int) -> bool:
    """'@' 前面不能是邮箱/路径里的字符，避免把 a@b、../x@y 当成提及。"""
    if pos == 0:
        return True
    prev = text[pos - 1]
    return not (prev.isascii() and (prev.isalnum() or prev in "._-@\\/"))


def scan_mentions(
    text: str, settings: Dict[str, Any]
) -> List[Tuple[Dict[str, Any], int, int]]:
    """按出现顺序找出文本里所有能对上成员的 @ 提及，返回 [(成员, 起, 止)]。

    不要求提及挨在一起：用户常写成「@agy 你来做，@DS4 @HY4 负责审核」。
    """
    hits: List[Tuple[Dict[str, Any], int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "@" and _mention_start_ok(text, i):
            emp, end = _match_mention_at(text, i, settings)
            if emp is not None:
                hits.append((emp, i, end))
                i = end
                continue
        i += 1
    return hits


def parse_collab(content: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """解析 /collab：整条消息里第一个能对上的 @ 是主创，其余是协助成员。

    早期实现要求所有 @ 紧跟在指令后面，用户写成
    `/collab @agy 你来做，@DS4 @HY4 负责审核` 就会被判成「缺少协助成员」，
    而 `@//agy/` 这种文件引用写法连主创都认不出来。现在按出现顺序扫描整条消息，
    任务内容 = 去掉提及后的文字。缺主创、缺协助成员、缺任务内容仍然一律拦截。

    返回 {"ok": bool, "error": str, "owner_id", "reviewer_ids", "goal"}。
    """
    result: Dict[str, Any] = {
        "ok": False, "error": "", "owner_id": "", "reviewer_ids": [], "goal": ""
    }
    rest = re.sub(r"^/collab\s*", "", content or "", count=1, flags=re.IGNORECASE).lstrip()
    if not rest:
        result["error"] = "缺少主创和协助成员：格式 /collab @主创 @协助成员 任务内容"
        return result

    hits = scan_mentions(rest, settings)
    if not hits:
        result["error"] = "缺少主创：请用 /collab @主创 @协助成员 任务内容"
        return result

    owner_id = str(hits[0][0]["id"])
    reviewer_ids: List[str] = []
    for emp, _start, _end in hits[1:]:
        eid = str(emp["id"])
        if eid != owner_id and eid not in reviewer_ids:
            reviewer_ids.append(eid)

    # 任务内容 = 去掉所有提及后的文字（连带去掉提及两边的标点碎片）
    pieces: List[str] = []
    last = 0
    for _emp, start, end in hits:
        pieces.append(rest[last:start])
        last = end
    pieces.append(rest[last:])
    goal = re.sub(r"\s+", " ", "".join(pieces)).strip()
    # 摘掉提及后常在标点前留一个空格（"参考 ，负责审核"），顺手收掉
    goal = re.sub(r"\s+([，。、；：！？）】」])", r"\1", goal)
    goal = re.sub(r"([，。、；：！？])\s+", r"\1", goal)
    goal = goal.strip(" ，,、;；:：")

    if not reviewer_ids:
        result["error"] = "缺少协助成员：主创之后至少再指定一位协助成员"
        return result
    if not goal:
        result["error"] = "缺少任务内容：/collab @主创 @协助成员 之后要写要做什么"
        return result
    # 成员可用性也在这里拦：原先只校验主创，被 @ 的协助成员若已停用，任务照样发起，
    # 直到审核环节才落一个「成员不可用」，主创白白交付一轮
    if not usable_member(settings, owner_id):
        result["error"] = (
            f"主创 {_member_label(settings, owner_id)} 已停用或缺少 CLI 配置："
            "请先在设置里启用后再发起协助任务"
        )
        return result
    unusable = [rid for rid in reviewer_ids if not usable_member(settings, rid)]
    if unusable:
        names = "、".join(_member_label(settings, rid) for rid in unusable)
        result["error"] = (
            f"协助成员 {names} 已停用或缺少 CLI 配置：请先在设置里启用后再发起协助任务"
        )
        return result

    result.update(ok=True, owner_id=owner_id, reviewer_ids=reviewer_ids, goal=goal)
    return result


def parse_single(content: str, settings: Dict[str, Any]) -> Optional[str]:
    """普通消息里找被 @ 的成员，返回成员 id；没有点名返回 None。

    按出现顺序取第一个：一条消息里 @ 了多人时，用户点名的第一位才是指令对象
    （早期实现按「别名最长」挑，会跳过第一个 @ 去执行后面的成员）。
    """
    hits = scan_mentions(content or "", settings)
    return str(hits[0][0]["id"]) if hits else None


def last_ai_member(messages: List[Dict[str, Any]], settings: Dict[str, Any]) -> Optional[str]:
    """最近一个跟用户说过话、并且现在还能用的 AI 成员。

    用于「消息里没 @ 任何人」时的默认对象：聊天窗里默认接着刚才那位说，
    比回一句「请 @ 成员」更符合直觉。只认本场会话里别人真正回过话的成员，
    已停用 / 缺 CLI 配置的一律跳过（否则消息发出去直接建个必然失败的任务）。
    """
    for msg in reversed(messages or []):
        if not isinstance(msg, dict):
            continue
        sender = str(msg.get("sender") or "")
        if not sender or sender in ("user", "system"):
            continue
        if usable_member(settings, sender):
            return sender
    return None


# ---------------------------------------------------------------------------
# 运行规程与 prompt
# ---------------------------------------------------------------------------

def _read_protocol(relative: str) -> str:
    path = os.path.join(PROMPTS_DIR, relative)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


ROLE_FILES = {
    TYPE_SINGLE: "roles/single.md",
    "owner_exec": "roles/owner.md",
    "owner_deliver": "roles/owner.md",
    STAGE_REVIEW: "roles/reviewer.md",
}

# 会话背景的预算：最近 8 条、单条 600 字、合计 3000 字。
# 成员每次都是一个新的 CLI 子进程（无状态），所以「带多少历史」完全由这里决定：
# 全量历史既不必要也太贵，按主流 agent 的做法取「最近若干条 + 更早的摘要 + 产物指针」。
CONTEXT_MAX_MSGS = 8
CONTEXT_MSG_CHARS = 600
CONTEXT_TOTAL_CHARS = 3000
CONTEXT_SUMMARY_CHARS = 400


def _truncate(text: Any, limit: int) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def context_eligible(msg: Any) -> bool:
    """能不能进「会话背景」：系统播报、任务卡片、各环节执行记录都不进。

    `in_chat: false` 的环节记录是给审计和「历次执行记录」看的，塞进背景只会挤占预算。
    """
    if not isinstance(msg, dict):
        return False
    if msg.get("in_chat") is False:
        return False
    if msg.get("kind") in ("task_card", "task_record"):
        return False
    if msg.get("sender") == "system":
        return False
    return bool(str(msg.get("content") or "").strip())


def build_context(
    messages: Optional[List[Dict[str, Any]]],
    current_msg_id: str = "",
    include_after: bool = False,
) -> Dict[str, Any]:
    """把会话消息压成一段合格的上下文：最近若干条 + 被截断部分的进展摘要。

    返回 {"messages": [{"name","content"}], "dropped": int, "summary": str}。
    默认只取当前这条用户消息之前的消息，避免把「用户原话」重复两遍。

    include_after=True 时保留这条消息之后的全部消息（仍然排除它自己）：
    重试沿用的是最初的用户原话，但那之后用户补充的约束必须一起带上，
    否则「点重试」等于按旧输入再烧一次 token。
    """
    eligible = [m for m in (messages or []) if context_eligible(m)]
    idx = None
    if current_msg_id:
        for i, msg in enumerate(eligible):
            if msg.get("id") == current_msg_id:
                idx = i
                break
    if idx is None:
        before = eligible
    elif include_after:
        before = [m for i, m in enumerate(eligible) if i != idx]
    else:
        before = eligible[:idx]

    # 从新到旧取，超总预算就丢弃更早的
    picked: List[Dict[str, str]] = []
    total = 0
    for msg in reversed(before[-CONTEXT_MAX_MSGS:]):
        content = _truncate(msg.get("content"), CONTEXT_MSG_CHARS)
        if picked and total + len(content) > CONTEXT_TOTAL_CHARS:
            break
        picked.append({
            "name": "我" if msg.get("sender") == "user" else str(msg.get("sender_name") or msg.get("sender") or "成员"),
            "content": content,
        })
        total += len(content)
    picked.reverse()

    dropped = before[:len(before) - len(picked)]
    summary = ""
    if dropped:
        # 复用成员已经产出过的内容当摘要，不额外调一次模型
        produced = [m for m in dropped if m.get("sender") not in ("user", "system")]
        if produced:
            summary = _truncate(produced[-1].get("content"), CONTEXT_SUMMARY_CHARS)
    return {"messages": picked, "dropped": len(dropped), "summary": summary}


def build_prompt(
    stage: str,
    goal: str,
    task_type: str = TYPE_SINGLE,
    artifacts: Optional[List[Dict[str, Any]]] = None,
    opinions: Optional[List[Dict[str, Any]]] = None,
    workspace_dir: str = "",
    quoted: str = "",
    context: Optional[Dict[str, Any]] = None,
) -> str:
    """拼装某环节的完整输入；规程文件随软件提供，服务端读取后注入。

    context 见 build_context()：会话背景 + 产物清单一起构成规格 4.2 的「会话背景、产物索引」。
    """
    if stage == STAGE_REVIEW:
        role_key = STAGE_REVIEW
        stage_label = "协助审核（只读）"
    elif stage == STAGE_DELIVER:
        role_key = "owner_deliver"
        stage_label = "主创交付"
    elif task_type == TYPE_COLLAB:
        role_key = "owner_exec"
        stage_label = "主创执行"
    else:
        role_key = TYPE_SINGLE
        stage_label = "单任务执行"

    parts = [_read_protocol("protocol/task-flow.md"), _read_protocol(ROLE_FILES[role_key])]
    parts.append(f"【当前环节】{stage_label}")
    parts.append(f"【工作目录】{workspace_dir or '（未指定）'}")
    if context and context.get("messages"):
        head = f"【会话背景（最近 {len(context['messages'])} 条）】"
        if context.get("dropped"):
            head = f"【会话背景（最近 {len(context['messages'])} 条，更早的 {context['dropped']} 条已省略）】"
        lines = [f"[{item['name']}] {item['content']}" for item in context["messages"]]
        if context.get("summary"):
            lines.append(f"更早的进展：{context['summary']}")
        parts.append(head + "\n" + "\n".join(lines))
    parts.append(f"【用户原话】\n{goal}")
    if quoted:
        parts.append(f"【被引用的原话】\n{quoted}")
    if artifacts:
        parts.append("【主创产物清单】\n" + json.dumps(artifacts, ensure_ascii=False, indent=2))
    if opinions:
        lines = []
        for o in opinions:
            mark = ""
            if o.get("review_verdict") == "pass":
                # 判定通过的成员没有改动要求：明说出来，免得主创硬找点东西改
                mark = "（[通过]，无需改动）"
            elif o.get("review_verdict") == "issues":
                mark = "（[意见]）"
            lines.append(f"- {o.get('name') or o.get('member_id')}{mark}: {o.get('summary') or ''}")
        parts.append("【审核意见】\n" + "\n".join(lines))
    if stage == STAGE_REVIEW:
        # 审核是只读环节、不会产出文件。若沿用通用那句「如果产出了文件才登记」，
        # 模型会以为不必写登记块，[通过] 与条数就落不到卡片上。
        parts.append(
            "【输出要求】正文用正常文字写审核意见（这段会直接展示给用户，不要写成 JSON）："
            "首行写 `[通过]` 或 `[意见]`，第二行写核对范围；有意见时按「隐患与漏洞」→「新意与巧思」分节列条目。"
            "末尾务必附一个 JSON 代码块登记（软件会摘掉，不展示）："
            '{"artifacts": [{"path": "...", "summary": "..."}], '
            '"review": {"verdict": "pass|issues", "issues": 0, "ideas": 0}}。'
            "verdict 与首行标记一致，issues / ideas 填实际条数；即使没有产出文件也要写这一段。"
        )
    else:
        parts.append(
            "【输出要求】正文用正常文字回答（这段会直接展示给用户，不要写成 JSON）；"
            "如果产出了文件，在正文末尾附一个 JSON 代码块登记产物清单："
            '{"artifacts": [{"path": "...", "summary": "...", "bytes": 0}]}，'
            "交付环节可在其中加 changes 字段说明改动。软件会把这段登记从正文里摘掉。"
        )
    return "\n\n".join(p for p in parts if p)


_SUMMARY_FIELDS = (
    "summary", "result", "output", "content", "text", "answer",
    "opinion", "comment", "说明", "结论",
)


def _loads_json_object(text: str) -> Optional[Dict[str, Any]]:
    """从一段文本里取出一个 JSON 对象，容忍前后有说明文字或 markdown 围栏。"""
    s = (text or "").strip()
    if not s:
        return None
    try:
        data = json.loads(s)
        if isinstance(data, dict):
            return data
    except (ValueError, TypeError):
        pass
    start = s.find("{")
    while start != -1:
        try:
            data = json.loads(s[start:])
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            pass
        # 整体解析失败（后面还跟着别的字），退一步只截到最后一个右括号
        end = s.rfind("}")
        if end > start:
            try:
                data = json.loads(s[start:end + 1])
                if isinstance(data, dict):
                    return data
            except (ValueError, TypeError):
                pass
        start = s.find("{", start + 1)
    return None


def _readable_summary(data: Dict[str, Any]) -> str:
    """JSON 里没有 summary 字段时，也拼出人能读的一段，而不是把花括号丢给用户。"""
    for key in _SUMMARY_FIELDS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    lines = []
    for key, value in data.items():
        if key in ("artifacts", "changes", "opinion_handling"):
            continue
        if isinstance(value, str) and value.strip():
            lines.append(f"{key}：{value.strip()}")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            lines.append(f"{key}：{value}")
    return "\n".join(lines[:12])


def _clean_artifacts(raw_artifacts: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_artifacts, list):
        return []
    clean = []
    for item in raw_artifacts:
        if isinstance(item, dict) and item.get("path"):
            clean.append({
                "path": str(item["path"]),
                "summary": str(item.get("summary") or ""),
                "bytes": int(item.get("bytes") or 0),
            })
    return clean


def _clean_review(raw: Any) -> Dict[str, Any]:
    """审核登记块：只认 verdict=pass/issues，其余字段一律不采信，避免模型自造判定。"""
    if not isinstance(raw, dict):
        return {}
    verdict = str(raw.get("verdict") or "").strip().lower()
    if verdict not in ("pass", "issues"):
        return {}

    def count(key: str) -> int:
        try:
            return max(0, int(raw.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    return {"verdict": verdict, "issues": count("issues"), "ideas": count("ideas")}


def parse_output(text: str) -> Dict[str, Any]:
    """解析结构化输出：正文照原样展示，只把结尾的产物登记块摘出来。

    协议要求「正文用自然语言写，产物清单用 JSON 代码块登记」，所以这里先剥登记块、
    再认整段就是 JSON 的老写法。任何情况下都不把 `{...}` 原样当正文递给聊天窗口。
    """
    raw = text or ""
    body = raw.strip()
    data: Optional[Dict[str, Any]] = None

    # 1) 正文 + 结尾登记块：把登记块从正文里摘掉，正文就是要展示的内容
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.S | re.I):
        candidate = _loads_json_object(match.group(1))
        if not candidate:
            continue
        # review 也是登记块：审核成员只给 verdict 而不列 artifacts 时，同样要摘掉，
        # 否则这段 JSON 会原样出现在聊天卡片里
        if not ({"artifacts", "changes", "summary", "opinion_handling", "review"} & set(candidate)):
            continue
        data = candidate
        body = (raw[:match.start()] + raw[match.end():]).strip()
        break

    # 2) 整段就是一个 JSON（老写法，或模型只回了一份结构）
    if data is None:
        whole = _loads_json_object(raw)
        if whole is not None:
            data = whole
            body = ""

    # 正文才是给人看的内容：有正文就用正文，只有整段都是 JSON 时才从字段里取
    summary = body if body else (_readable_summary(data) if data else "")
    if not summary:
        summary = raw.strip()

    review = _clean_review((data or {}).get("review"))
    if not review and body.strip():
        # 模型漏写登记块时，仍按正文首行的 [通过] / [意见] 兜底判定。
        # 条数不可信，统一记 0，卡片只用 verdict。
        first_line = body.strip().splitlines()[0].strip()
        if first_line.startswith("[通过]"):
            review = {"verdict": "pass", "issues": 0, "ideas": 0}
        elif first_line.startswith("[意见]"):
            review = {"verdict": "issues", "issues": 0, "ideas": 0}

    return {
        "summary": summary,
        "artifacts": _clean_artifacts((data or {}).get("artifacts")),
        "changes": str((data or {}).get("changes") or (data or {}).get("opinion_handling") or "").strip(),
        "review": review,
    }


def _review_needs_retry(res: Any) -> bool:
    """审核回答是否需要语义重试：回了内容，却没给出任何结论（开场白 / 只剩工具标记）。

    真空输出不算——那交给「无意见」，重试也大概率还是空。只有「说了话但没结论」
    才值得带强指令再问一次，避免把「我先读取…」当成审核意见入库。
    """
    if str(getattr(res, "status", "") or "") != "success":
        return False
    raw = str(getattr(res, "output", "") or "")
    cleaned = sanitize_model_output(raw)
    if not cleaned.strip():
        return False
    parsed = parse_output(raw)
    if parsed.get("review"):
        return False  # 已给出 [通过]/[意见] 或登记块，属于有效结论
    return looks_like_incomplete(cleaned)


def scan_artifacts(workspace_dir: str, artifacts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """产物清单里没写大小时从工作目录补齐；文件不存在也照实列出。"""
    result = []
    for item in artifacts or []:
        entry = dict(item)
        path = str(entry.get("path") or "")
        if path and not os.path.isabs(path) and workspace_dir:
            full = os.path.join(workspace_dir, path)
        else:
            full = path
        if full and os.path.isfile(full) and not entry.get("bytes"):
            try:
                entry["bytes"] = os.path.getsize(full)
            except OSError:
                pass
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
# 任务卡片
# ---------------------------------------------------------------------------

def build_card(task: Dict[str, Any]) -> Dict[str, Any]:
    part_a = task.get("part_a") or {}
    part_b = task.get("part_b") or []
    return {
        "kind": "task_card",
        "task_id": task.get("task_id"),
        "goal": task.get("goal", ""),
        "type": task.get("type"),
        "status": task.get("status"),
        "step": task.get("step"),
        "owner": {"id": task.get("owner_id"), "name": task.get("owner_name")},
        "owner_name": task.get("owner_name"),
        "reviewer_names": task.get("reviewer_names") or [],
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        # 任务创建时刻：卡片头部直接显示，老会话没有这个字段时前端退回落库消息的时间
        "created_at": task.get("created_at"),
        # 这一版终稿是怎么来的："" = 正常流程 / 用户手点，"review_auto" = 所有审核
        # 出结论后系统自动补的那一次。交付段会挂一个「审核补全后重新生成」标签。
        "deliver_source": task.get("deliver_source", ""),
        "deliver_source_at": task.get("deliver_source_at"),
        # 审核意见重出之后终稿就过期了：自动补交付期间短暂为真，重出成功后清掉；
        # 审核没齐时留着，卡片上提示「建议重新生成交付」
        "deliver_stale": bool(task.get("deliver_stale")),
        "flow": list(task.get("flow") or []),
        "artifacts": list(task.get("artifacts") or []),
        "part_a": {
            "brief": part_a.get("brief", ""),
            "artifacts": list(part_a.get("artifacts") or []),
            "final": dict(part_a.get("final") or {}),
            "status": part_a.get("status", MEMBER_WAITING),
            "elapsed": part_a.get("elapsed", 0),
            "started_at": part_a.get("started_at"),
            "finished_at": part_a.get("finished_at"),
            "msg_id": part_a.get("msg_id"),
            "sent_prompt": part_a.get("sent_prompt", ""),
            "raw_output": part_a.get("raw_output", ""),
            "final_status": part_a.get("final_status", MEMBER_WAITING),
            "final_elapsed": part_a.get("final_elapsed", 0),
            "final_started_at": part_a.get("final_started_at"),
            "final_finished_at": part_a.get("final_finished_at"),
            "final_sent_prompt": part_a.get("final_sent_prompt", ""),
            "final_raw_output": part_a.get("final_raw_output", ""),
            "final_source": part_a.get("final_source", ""),
        },
        "part_b": [dict(item) for item in part_b],
    }


# ---------------------------------------------------------------------------
# 任务管理器
# ---------------------------------------------------------------------------

class TaskManager:
    def __init__(self, store: Any = None, runner: Any = None, killer: Any = None,
                 auto_start: bool = True):
        self.store = store or storage
        self.runner = runner or Executor.run_employee
        self.killer = killer or Executor.kill_session_process
        # 测试里可关掉自动调度，先检查刚入队的任务结构与卡片
        self.auto_start = auto_start
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._workers: Dict[Tuple[str, str], threading.Thread] = {}
        # 每成员「正在执行」的占用标记：队列任务与审核、重试都要先占用它，保证同一成员串行
        self._busy: Dict[Tuple[str, str], bool] = {}
        self._busy_owner: Dict[Tuple[str, str], int] = {}
        self._busy_depth: Dict[Tuple[str, str], int] = {}
        # worker 代号：队列空时用它确认「没有新的任务在我退出前入队」
        self._gen: Dict[Tuple[str, str], int] = {}
        self._active_keys: Dict[str, Dict[str, Any]] = {}
        # 正在重跑的环节：挡住连点两次「重试」造成的重复执行
        self._retries: set = set()
        # 执行器是否支持流式回调（on_chunk）。测试里的假执行器通常不带，自动退回非流式，
        # 免得为了一个进度提示去改一整套测试夹具
        self._streams = _accepts_on_chunk(self.runner)

    def _stream_sink(self, sid: str, tid: str, stage: str, member_id: str, key: str):
        """把执行器的增量回调转成「这场会话、这位成员正在写什么」。"""
        def sink(text: str, info: Optional[Dict[str, Any]] = None) -> None:
            data = info or {}
            streaming.publish(
                sid, key, tid, stage, member_id,
                text or "",
                str(data.get("phase") or ""),
                str(data.get("activity") or ""),
            )
        return sink

    # ---------------- 会话读写小工具 ----------------
    def _get(self, sid: str) -> Optional[Dict[str, Any]]:
        return self.store.get_session(sid)

    def _save(self, session: Dict[str, Any]) -> None:
        self.store.save_session(session)

    def _update_card(self, session: Dict[str, Any], task: Dict[str, Any]) -> None:
        for msg in session.get("messages") or []:
            if isinstance(msg, dict) and msg.get("id") == task.get("card_msg_id"):
                msg["card"] = build_card(task)
                msg["status"] = task.get("status")
                break

    def _add_flow(self, task: Dict[str, Any], text: str) -> None:
        task.setdefault("flow", []).append({"at": int(time.time()), "text": text})

    # ---------------- 提交任务 ----------------
    def submit_message(
        self,
        sid: str,
        content: str,
        settings: Dict[str, Any],
        reply_to: Optional[str] = None,
        forced_owner_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """处理一条用户消息：记录原话、按指令/点名创建任务。"""
        content = (content or "").strip()
        with self.store.session_lock(sid):
            session = self._get(sid)
            if not session:
                return {"error": "会话不存在"}
            ensure_task_fields(session)
            messages: List[Dict[str, Any]] = session.setdefault("messages", [])

            quoted = ""
            if reply_to:
                for old in messages:
                    if isinstance(old, dict) and old.get("id") == reply_to:
                        quoted = str(old.get("content") or "")[:2000]
                        break

            user_msg = {
                "id": msg_id(),
                "sender": "user",
                "sender_name": "我",
                "content": content,
                "in_chat": True,
                "created_at": int(time.time()),
            }
            if quoted:
                user_msg["quote"] = {"id": reply_to, "sender_name": "原消息", "excerpt": quoted[:200]}
            messages.append(user_msg)

            is_collab = bool(re.match(r"^/collab(\s|$)", content, flags=re.I))
            if is_collab:
                parsed = parse_collab(content, settings)
                if not parsed["ok"]:
                    self._save(session)
                    return {"error": parsed["error"], "validation_error": True}
                owner_id = parsed["owner_id"]
                reviewer_ids = parsed["reviewer_ids"]
                goal = parsed["goal"]
                task_type = TYPE_COLLAB
            else:
                # 引用回复等价于 @ 被回复的成员（文档 2 / 5.1）
                owner_id = forced_owner_id or parse_single(content, settings) or ""
                reviewer_ids = []
                goal = content
                task_type = TYPE_SINGLE
                if not owner_id:
                    # 没 @ 任何人：默认接着「最近一位回过话的成员」说（微信式对话习惯）。
                    # 只取本场会话里现存、可用的成员；一个都没有才退回「仅作上下文」。
                    owner_id = last_ai_member(messages[:-1], settings) or ""
                    if owner_id:
                        emp_default = self._employee(settings, owner_id) or {}
                        user_msg["auto_to"] = {
                            "id": owner_id,
                            "name": emp_default.get("name") or owner_id,
                        }

            if not owner_id:
                # 没有点名、也没有可接话的成员：只作为上下文保留，不创建任务
                self._save(session)
                return {"info": "context_only"}

            emp = self._employee(settings, owner_id)
            if not emp:
                self._save(session)
                return {"error": "被点名的成员不可用（已停用或缺少 CLI 配置）"}

            tid = task_id()
            now = int(time.time())
            task: Dict[str, Any] = {
                "task_id": tid,
                "type": task_type,
                "source_msg_id": user_msg["id"],
                "goal": goal,
                "owner_id": owner_id,
                "owner_name": emp.get("name") or owner_id,
                "reviewer_ids": reviewer_ids,
                "reviewer_names": [
                    (self._employee(settings, rid) or {}).get("name") or rid for rid in reviewer_ids
                ],
                "status": STATUS_QUEUED,
                "step": 1,
                "artifacts": [],
                "card_msg_id": "",
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "quoted": quoted,
                "flow": [{"at": now, "text": "任务已创建，进入队列"}],
                "part_a": {
                    "brief": "", "artifacts": [], "final": {},
                    "status": MEMBER_WAITING, "final_status": MEMBER_WAITING,
                    "elapsed": 0, "final_elapsed": 0,
                    # 每个环节自己的起止时刻：任务级别的 started_at/finished_at
                    # 分不到三段上，卡片和「输入输出」要显示「几点开始、几点返回」
                    "started_at": None, "finished_at": None,
                    "final_started_at": None, "final_finished_at": None,
                    "msg_id": "", "sent_prompt": "", "raw_output": "",
                    "final_sent_prompt": "", "final_raw_output": "",
                },
                "part_b": [],
                "attempts": {},
            }

            if task_type == TYPE_COLLAB:
                for rid in reviewer_ids:
                    rname = (self._employee(settings, rid) or {}).get("name") or rid
                    task["part_b"].append({
                        "member_id": rid, "name": rname, "status": MEMBER_WAITING,
                        "summary": "", "raw_output": "", "sent_prompt": "", "msg_id": "",
                        "elapsed": 0, "started_at": None, "finished_at": None,
                    })
                card_msg = {
                    "id": msg_id(),
                    "sender": "system",
                    "sender_name": "任务卡片",
                    "content": goal,
                    "kind": "task_card",
                    "task_id": tid,
                    "in_chat": True,
                    "status": STATUS_QUEUED,
                    "created_at": now,
                    "card": build_card(task),
                }
                messages.append(card_msg)
                task["card_msg_id"] = card_msg["id"]
            else:
                result_msg = {
                    "id": msg_id(),
                    "sender": owner_id,
                    "sender_name": emp.get("name") or owner_id,
                    "content": "",
                    "kind": "task_result",
                    "task_id": tid,
                    "in_chat": True,
                    "status": STATUS_QUEUED,
                    "created_at": now,
                    "sent_prompt": "",
                    "raw_response": "",
                    "artifacts": [],
                }
                messages.append(result_msg)
                task["result_msg_id"] = result_msg["id"]

            session["tasks"][tid] = task
            session["task_queues"].setdefault(owner_id, []).append(tid)
            session["active_task_id"] = tid
            self._save(session)

        if self.auto_start:
            self.ensure_worker(sid, owner_id)
        return {"task_id": tid, "type": task_type}

    @staticmethod
    def _employee(settings: Dict[str, Any], emp_id: str) -> Optional[Dict[str, Any]]:
        return usable_member(settings, emp_id)

    # ---------------- 每成员 FIFO worker ----------------
    def ensure_worker(self, sid: str, member_id: str) -> None:
        key = (sid, member_id)
        with self._lock:
            # 每次呼叫都推进代号：正在退出的 worker 会看到代号变化，回去再查一次队列，
            # 避免「它判断队列空了 → 新任务入队 → ensure_worker 看到线程还活着就不新开」导致任务永远排队
            self._gen[key] = self._gen.get(key, 0) + 1
            gen = self._gen[key]
            thread = self._workers.get(key)
            if thread and thread.is_alive():
                return
            thread = threading.Thread(
                target=self._worker_loop, args=(sid, member_id, gen),
                name=f"task-worker-{sid}-{member_id}", daemon=True,
            )
            self._workers[key] = thread
            thread.start()

    def _worker_loop(self, sid: str, member_id: str, gen: int = 0) -> None:
        key = (sid, member_id)
        try:
            while True:
                with self.store.session_lock(sid):
                    session = self._get(sid)
                    if not session:
                        return
                    ensure_task_fields(session)
                    queue = session["task_queues"].get(member_id) or []
                    tid = ""
                    for candidate in queue:
                        task = session["tasks"].get(candidate)
                        if task and task.get("status") == STATUS_QUEUED:
                            tid = candidate
                            break
                    if not tid:
                        queue[:] = [
                            t for t in queue
                            if t in session["tasks"] and session["tasks"][t].get("status") not in FINAL_TASK_STATES
                        ]
                        self._save(session)

                if not tid:
                    with self._lock:
                        if self._gen.get(key, 0) != gen:
                            # 退出前有人入队并呼叫了 ensure_worker，回去再查一次
                            gen = self._gen[key]
                            continue
                        if self._workers.get(key) is threading.current_thread():
                            self._workers.pop(key, None)
                        return

                # 队列任务之间由这个 worker 线程天然串行；与审核 / 重试的互斥改由
                # 每个执行单元自己占用成员（见 _execute）。早期版本在这里持有主创占用直到
                # 整个任务（含审核）结束，交叉协作（A 主创 agy/审 DS4 × B 主创 DS4/审 agy）
                # 会互相等对方手里的成员，死锁到等待上限。
                with self.store.session_lock(sid):
                    session = self._get(sid)
                    if not session:
                        return
                    task = (session.get("tasks") or {}).get(tid)
                    started = bool(task and task.get("status") == STATUS_QUEUED)
                    if started:
                        task["status"] = STATUS_RUNNING
                        task["started_at"] = int(time.time())
                        # 流水给人看，用成员名而不是 emp_xxx 这种内部 id
                        self._add_flow(task, f"{task.get('owner_name') or member_id} 开始执行")
                        if task.get("result_msg_id"):
                            self._update_message(session, task["result_msg_id"], {"status": STATUS_RUNNING})
                        self._update_card(session, task)
                        self._save(session)
                if started:
                    self._run_task_safe(sid, tid)

                with self.store.session_lock(sid):
                    session = self._get(sid)
                    if not session:
                        return
                    ensure_task_fields(session)
                    queue = session["task_queues"].get(member_id) or []
                    if tid in queue:
                        queue.remove(tid)
                    self._save(session)
                    has_more = any(
                        (session["tasks"].get(t) or {}).get("status") == STATUS_QUEUED for t in queue
                    )
                if not has_more:
                    # 回到循环顶部走「队列为空」分支，那里有代号校验，不会被新入队的任务绕过
                    continue
        finally:
            with self._lock:
                if self._workers.get(key) is threading.current_thread():
                    self._workers.pop(key, None)

    # 同一成员最多等这么久；超时就不执行本次环节（宁可让用户看到失败并重试，
    # 也不并发起两个进程写同一工作目录）。
    MEMBER_WAIT_SECONDS = 1800

    def _member_display(self, member_id: str) -> str:
        """报错与流水里优先用成员名：`emp_agy` 这类内部 id 对人没意义。"""
        try:
            return _member_label(self.store.load_settings(), member_id)
        except Exception:
            return member_id

    def _acquire_member(self, sid: str, member_id: str, timeout: Optional[float] = None) -> bool:
        """占用某位成员（等待它空闲）。同一线程可重入，靠计数释放。"""
        key = (sid, member_id)
        ident = threading.get_ident()
        deadline = time.time() + (self.MEMBER_WAIT_SECONDS if timeout is None else timeout)
        with self._cond:
            if self._busy_owner.get(key) == ident:
                self._busy_depth[key] = self._busy_depth.get(key, 1) + 1
                return True
            while self._busy.get(key):
                remain = deadline - time.time()
                if remain <= 0:
                    # 绝不为了「不死锁」就并发起进程：同一成员两个 CLI 同时写同一工作目录
                    # 会互相覆盖产物。放弃本次执行，由调用方把环节标成失败并允许重试。
                    print(f"[TaskManager] 等 {self._member_display(member_id)} 空闲超时，本次不执行")
                    return False
                self._cond.wait(timeout=min(0.2, remain))
            self._busy[key] = True
            self._busy_owner[key] = ident
            self._busy_depth[key] = 1
            return True

    def _release_member(self, sid: str, member_id: str) -> None:
        key = (sid, member_id)
        ident = threading.get_ident()
        with self._cond:
            if self._busy_owner.get(key) != ident:
                return
            depth = self._busy_depth.get(key, 1) - 1
            if depth > 0:
                self._busy_depth[key] = depth
                return
            self._busy[key] = False
            self._busy_depth.pop(key, None)
            self._busy_owner.pop(key, None)
            self._cond.notify_all()

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """测试辅助：等所有执行单元结束。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not self._active_keys:
                    return True
            time.sleep(0.02)
        return False

    # ---------------- 任务执行 ----------------
    def _run_task_safe(self, sid: str, tid: str) -> None:
        """worker 里调用：异常不能让线程静默死掉、把任务永远留在「执行中」。"""
        try:
            self._run_task(sid, tid)
        except Exception as exc:
            print(f"[TaskManager] 任务 {tid} 执行异常: {exc}")
            with self.store.session_lock(sid):
                session = self._get(sid)
                if not session:
                    return
                task = (session.get("tasks") or {}).get(tid)
                if not task or task.get("status") != STATUS_RUNNING:
                    return
                task["status"] = STATUS_FAILED
                task["finished_at"] = int(time.time())
                self._add_flow(task, "执行异常，任务失败")
                self._update_message(session, task.get("result_msg_id"), {
                    "status": STATUS_FAILED,
                    "content": f"执行异常：{exc}",
                })
                self._update_card(session, task)
                self._save(session)

    def _run_task(self, sid: str, tid: str) -> None:
        with self.store.session_lock(sid):
            session = self._get(sid)
            if not session:
                return
            task = (session.get("tasks") or {}).get(tid)
            if not task:
                return
            task_type = task.get("type")
            settings = self.store.load_settings()
            workspace_dir = session.get("workspace_dir", "")

        if task_type == TYPE_SINGLE:
            self._run_single(sid, tid, settings, workspace_dir)
            return

        # 协助任务：主创执行 → 并行审核 → 主创交付
        ok = self._run_exec(sid, tid, settings, workspace_dir)
        if not ok:
            return
        self._run_reviews(sid, tid, settings, workspace_dir)
        self._run_deliver(sid, tid, settings, workspace_dir)

    def _run_single(self, sid: str, tid: str, settings: Dict[str, Any], workspace_dir: str) -> None:
        result = self._execute(sid, tid, STAGE_EXEC, None, settings, workspace_dir)
        with self.store.session_lock(sid):
            session = self._get(sid)
            if not session:
                return
            task = (session.get("tasks") or {}).get(tid)
            if not task:
                return
            if result.get("cancelled"):
                status = STATUS_CANCELLED
            elif result.get("status") == STATUS_TIMEOUT:
                status = STATUS_TIMEOUT
            elif result.get("status") == STATUS_FAILED:
                status = STATUS_FAILED
            elif task.get("status") == STATUS_CANCELLED:
                # 取消发生在「进程刚跑完、结果还没落库」的窗口里：以取消为准，终态不回退
                status = STATUS_CANCELLED
            else:
                status = STATUS_DELIVERED
            task["status"] = status
            task["finished_at"] = int(time.time())
            task["artifacts"] = scan_artifacts(workspace_dir, result.get("artifacts") or [])
            task["part_a"]["brief"] = result.get("summary", "")
            task["part_a"]["artifacts"] = task["artifacts"]
            # 单任务的 part_a 也要收尾，否则交付后仍停在 in_progress，
            # 服务重启时会被恢复逻辑误判成「被中断的环节」
            task["part_a"]["status"] = status
            task["part_a"]["elapsed"] = result.get("elapsed", 0)
            task["part_a"]["started_at"] = result.get("started_at")
            task["part_a"]["finished_at"] = result.get("finished_at")
            self._add_flow(task, f"单任务{self._state_label(status)}")
            self._update_message(session, task.get("result_msg_id"), {
                "content": result.get("summary", ""),
                "status": status,
                "sent_prompt": result.get("prompt", ""),
                "raw_response": result.get("raw", ""),
                "artifacts": task["artifacts"],
                "elapsed_time": result.get("elapsed", 0),
                "started_at": result.get("started_at"),
                "finished_at": result.get("finished_at"),
            })
            self._update_card(session, task)
            self._save(session)

    def _run_exec(self, sid: str, tid: str, settings: Dict[str, Any], workspace_dir: str) -> bool:
        result = self._execute(sid, tid, STAGE_EXEC, None, settings, workspace_dir)
        with self.store.session_lock(sid):
            session = self._get(sid)
            if not session:
                return False
            task = (session.get("tasks") or {}).get(tid)
            if not task:
                return False
            part_a = task.setdefault("part_a", {})
            if not result.get("cancelled") and task.get("status") == STATUS_CANCELLED:
                # 取消落在「进程已返回、结果未落库」的窗口：以取消为准，终态不回退
                result = dict(result, cancelled=True)
            if result.get("cancelled"):
                task["status"] = STATUS_CANCELLED
                part_a["status"] = STATUS_CANCELLED
                part_a["elapsed"] = result.get("elapsed", 0)
                part_a["started_at"] = result.get("started_at")
                part_a["finished_at"] = result.get("finished_at")
                self._add_flow(task, "主创执行已取消")
            elif result.get("status") in (STATUS_FAILED, STATUS_TIMEOUT):
                part_a["status"] = result["status"]
                part_a["elapsed"] = result.get("elapsed", 0)
                part_a["started_at"] = result.get("started_at")
                part_a["finished_at"] = result.get("finished_at")
                task["status"] = result["status"]
                self._add_flow(task, "主创执行" + self._state_label(result["status"]))
            else:
                task["artifacts"] = scan_artifacts(workspace_dir, result.get("artifacts") or [])
                part_a.update({
                    "brief": result.get("summary", ""),
                    "artifacts": task["artifacts"],
                    "status": STATUS_DONE,
                    "msg_id": result.get("record_msg_id", ""),
                    "sent_prompt": result.get("prompt", ""),
                    "raw_output": result.get("raw", ""),
                    # 卡片上要显示「这一段跑了多久」，只有环节自己有耗时，
                    # 任务级别的 started_at/finished_at 分不到三段
                    "elapsed": result.get("elapsed", 0),
                    "started_at": result.get("started_at"),
                    "finished_at": result.get("finished_at"),
                })
                task["step"] = 2
                self._add_flow(task, "主创执行完成，开始并行审核")
            self._update_card(session, task)
            self._save(session)
        return not result.get("cancelled") and result.get("status") not in (STATUS_FAILED, STATUS_TIMEOUT)

    def _run_reviews(self, sid: str, tid: str, settings: Dict[str, Any], workspace_dir: str) -> None:
        with self.store.session_lock(sid):
            session = self._get(sid)
            if not session:
                return
            task = (session.get("tasks") or {}).get(tid)
            if not task:
                return
            reviewer_ids = [item["member_id"] for item in task.get("part_b") or []]
            self._add_flow(task, f"{len(reviewer_ids)} 位成员开始并行审核")
            self._save(session)

        if not reviewer_ids:
            return
        with ThreadPoolExecutor(max_workers=len(reviewer_ids)) as pool:
            futures = [
                pool.submit(self._run_review_one, sid, tid, rid, settings, workspace_dir)
                for rid in reviewer_ids
            ]
            for future in futures:
                try:
                    future.result()
                except Exception as exc:  # 单个审核失败不影响主创交付
                    print(f"[TaskManager] 审核异常: {exc}")

        with self.store.session_lock(sid):
            session = self._get(sid)
            if not session:
                return
            task = (session.get("tasks") or {}).get(tid)
            if not task:
                return
            task["step"] = 3
            self._add_flow(task, "审核结束，主创开始交付")
            self._update_card(session, task)
            self._save(session)

    def _run_review_one(
        self, sid: str, tid: str, member_id: str,
        settings: Dict[str, Any], workspace_dir: str,
    ) -> None:
        # 占用由 _execute 统一处理（同一成员串行）；这里不再重复占用，
        # 重复占用会让「重试线程先占用 → 审核内部再等自己空闲」的自我等待问题重现
        result = self._execute(
            sid, tid, STAGE_REVIEW, member_id, settings, workspace_dir,
            read_only=True,
        )

        with self.store.session_lock(sid):
            session = self._get(sid)
            if not session:
                return
            task = (session.get("tasks") or {}).get(tid)
            if not task:
                return
            for item in task.get("part_b") or []:
                if item.get("member_id") != member_id:
                    continue
                if result.get("cancelled"):
                    item["status"] = STATUS_CANCELLED
                elif item.get("status") == STATUS_CANCELLED:
                    # 用户在这次审核返回前点了取消：状态以取消为准（意见不参与交付），
                    # 输出仍留在「历次执行记录」里可查
                    pass
                elif result.get("status") == STATUS_TIMEOUT:
                    item["status"] = STATUS_TIMEOUT
                elif result.get("status") == STATUS_FAILED:
                    item["status"] = STATUS_FAILED
                else:
                    summary_now = result.get("summary") or ""
                    review_now = result.get("review") or {}
                    if not summary_now.strip():
                        # 完全没输出：无意见
                        item["status"] = STATUS_NO_OPINION
                    elif not review_now.get("verdict") and looks_like_incomplete(summary_now):
                        # 只回了「我先读取…」这类开场白、没给结论（语义重试后仍如此）：
                        # 不能冒充「完成」，记无意见，免得把开场白当审核意见喂给主创
                        item["status"] = STATUS_NO_OPINION
                    else:
                        item["status"] = STATUS_DONE
                review = result.get("review") or {}
                summary_text = result.get("summary", "")
                # 无意见时把正文换成明确说明：气泡显示「未给出结论」，而不是把开场白当结论。
                # 原始输出仍保留在 raw_output，可在「交互详情」里查看。
                if item["status"] == STATUS_NO_OPINION:
                    if not (summary_text or "").strip():
                        summary_text = "（本轮未给出意见）"
                    elif looks_like_incomplete(summary_text):
                        summary_text = (
                            "（本轮未给出有效结论：模型只返回了开场白，"
                            "没有按要求给出 [通过] / [意见]。原始输出可在「交互详情」查看）"
                        )
                item.update({
                    "summary": summary_text,
                    "raw_output": result.get("raw", ""),
                    "sent_prompt": result.get("prompt", ""),
                    "msg_id": result.get("record_msg_id", ""),
                    "elapsed": result.get("elapsed", 0),
                    "started_at": result.get("started_at"),
                    "finished_at": result.get("finished_at"),
                    "review_verdict": review.get("verdict", ""),
                    "review_counts": {
                        "issues": int(review.get("issues") or 0),
                        "ideas": int(review.get("ideas") or 0),
                    },
                })
                if review.get("verdict") == "pass":
                    self._add_flow(task, f"{item.get('name')} 审核通过")
                else:
                    self._add_flow(task, f"{item.get('name')} 审核{self._state_label(item['status'])}")
                break
            self._update_card(session, task)
            self._save(session)

    def _run_deliver(
        self, sid: str, tid: str, settings: Dict[str, Any], workspace_dir: str,
        source: str = "",
    ) -> None:
        """重出终稿。

        source="review_auto" 表示这次是「所有审核都出了结论之后系统自动补的一次」，
        卡片交付段会标出「审核补全后重新生成」，和用户手点「重新生成交付」区分开。
        """
        result = self._execute(sid, tid, STAGE_DELIVER, None, settings, workspace_dir)
        with self.store.session_lock(sid):
            session = self._get(sid)
            if not session:
                return
            task = (session.get("tasks") or {}).get(tid)
            if not task:
                return
            part_a = task.setdefault("part_a", {})
            if not result.get("cancelled") and task.get("status") == STATUS_CANCELLED:
                result = dict(result, cancelled=True)
            if result.get("cancelled"):
                task["status"] = STATUS_CANCELLED
                part_a["final_status"] = STATUS_CANCELLED
                part_a["final_elapsed"] = result.get("elapsed", 0)
                part_a["final_started_at"] = result.get("started_at")
                part_a["final_finished_at"] = result.get("finished_at")
                part_a["final_source"] = ""
                task["deliver_source"] = ""
                task["deliver_source_at"] = None
            elif result.get("status") in (STATUS_FAILED, STATUS_TIMEOUT):
                task["status"] = result["status"]
                part_a["final_status"] = result["status"]
                part_a["final_elapsed"] = result.get("elapsed", 0)
                part_a["final_started_at"] = result.get("started_at")
                part_a["final_finished_at"] = result.get("finished_at")
                part_a["final_source"] = ""
                task["deliver_source"] = ""
                task["deliver_source_at"] = None
            else:
                task["artifacts"] = scan_artifacts(
                    workspace_dir, result.get("artifacts") or task.get("artifacts") or []
                )
                part_a["artifacts"] = task["artifacts"]
                part_a["final"] = {
                    "summary": result.get("summary", ""),
                    # 终版改动说明 = 成员自述如何处理审核意见。
                    # 产物清单看 part_a["artifacts"]，这里不再重复一份路径列表
                    # （早期叫 changed_files，实际装的是全部产物路径，名不符实且无人读取）
                    "opinion_handling": result.get("changes", ""),
                }
                part_a["final_status"] = STATUS_DONE
                part_a["final_sent_prompt"] = result.get("prompt", "")
                part_a["final_raw_output"] = result.get("raw", "")
                part_a["final_elapsed"] = result.get("elapsed", 0)
                part_a["final_started_at"] = result.get("started_at")
                part_a["final_finished_at"] = result.get("finished_at")
                task["status"] = STATUS_DELIVERED
                task["step"] = 3
                # 终稿已经按最新审核意见重出了，过期标记随之解除
                task.pop("deliver_stale", None)
                # 只有真的重出成功才挂来源标签：自动补交付失败/被取消时，
                # 旧终稿已被清空，不该还标着「审核补全后重新生成」
                if source:
                    task["deliver_source"] = source
                    task["deliver_source_at"] = int(time.time())
                    part_a["final_source"] = source
                else:
                    task["deliver_source"] = ""
                    task["deliver_source_at"] = None
                    part_a["final_source"] = ""
            # 自动补交付的占用无论成败都要解开，否则这一轮之后再重审就不会补了
            task.pop("auto_deliver_inflight", None)
            if source == "review_auto":
                self._add_flow(task, "审核结论已齐，系统自动补一次交付（终稿重新生成）")
            task["finished_at"] = int(time.time())
            self._add_flow(task, "主创交付" + self._state_label(task["status"]))
            self._update_card(session, task)
            self._save(session)

    # ---------------- 单个执行单元 ----------------
    def _task_owner(self, sid: str, tid: str, member_id: Optional[str] = None) -> str:
        """这个执行单元要占用哪位成员：审核用被指派的成员，其余用主创。"""
        if member_id:
            return member_id
        with self.store.session_lock(sid):
            session = self._get(sid)
            task = ((session or {}).get("tasks") or {}).get(tid) or {}
            return str(task.get("owner_id") or "")

    def _execute(
        self,
        sid: str,
        tid: str,
        stage: str,
        member_id: Optional[str],
        settings: Dict[str, Any],
        workspace_dir: str,
        read_only: bool = False,
    ) -> Dict[str, Any]:
        """占用成员后执行一个环节；占用不成功就不执行（绝不并发跑同一成员）。"""
        owner = self._task_owner(sid, tid, member_id)
        if owner and not self._acquire_member(sid, owner):
            return {
                "status": STATUS_FAILED,
                "cancelled": False,
                "summary": MEMBER_BUSY_SUMMARY,
                "raw": "",
                "prompt": "",
                "record_msg_id": "",
                "member_busy": True,
            }
        try:
            return self._execute_unit(sid, tid, stage, member_id, settings, workspace_dir, read_only)
        finally:
            if owner:
                self._release_member(sid, owner)

    def _execute_unit(
        self,
        sid: str,
        tid: str,
        stage: str,
        member_id: Optional[str],
        settings: Dict[str, Any],
        workspace_dir: str,
        read_only: bool = False,
    ) -> Dict[str, Any]:
        with self.store.session_lock(sid):
            session = self._get(sid)
            if not session:
                return {"cancelled": True}
            task = (session.get("tasks") or {}).get(tid)
            if not task:
                return {"cancelled": True}
            emp_id = member_id or task.get("owner_id")
            emp = self._employee(settings, emp_id or "")
            if not emp:
                return {"status": STATUS_FAILED, "summary": "成员不可用", "raw": "", "prompt": ""}
            key = execution_key(sid, tid, stage, emp_id or "")
            if key in CANCELLED_KEYS:
                # 排队 / 等成员期间已被取消：不要再起进程白烧一次 token；
                # 标记由本次执行消费掉，避免长期堆积
                with self._lock:
                    CANCELLED_KEYS.discard(key)
                return {
                    "status": "cancelled", "cancelled": True, "summary": "",
                    "raw": "", "prompt": "", "record_msg_id": "",
                }
            opinions = [
                item for item in (task.get("part_b") or [])
                if item.get("status") == STATUS_DONE and item.get("summary")
            ] if stage == STAGE_DELIVER else None
            attempt_key = f"{stage}:{member_id or task.get('owner_id')}"
            # 重试沿用的是最初的用户原话，但那之后用户补充的约束必须一起带上
            is_retry = task.setdefault("attempts", {}).get(attempt_key, 0) > 0
            prompt = build_prompt(
                stage=stage,
                goal=task.get("goal", ""),
                task_type=task.get("type", TYPE_SINGLE),
                artifacts=task.get("artifacts") or [],
                opinions=opinions,
                workspace_dir=workspace_dir,
                quoted=task.get("quoted") or "",
                context=build_context(
                    session.get("messages"),
                    task.get("source_msg_id") or "",
                    include_after=is_retry,
                ),
            )
            if read_only:
                # 只读环节（审核）把「到底能不能读文件」如实写进 prompt：
                # 工具被禁用的成员否则会回一句「我先读取…」就结束本轮，等于没审核。
                prompt = prompt + capability_block(emp)
            record = {
                "id": msg_id(),
                "sender": emp_id,
                "sender_name": emp.get("name") or emp_id,
                "content": "",
                "kind": "task_record",
                "task_id": tid,
                "stage": stage,
                "member_id": member_id,
                "in_chat": False,
                "status": STATUS_RUNNING,
                "sent_prompt": prompt,
                "raw_response": "",
                "created_at": int(time.time()),
            }
            session.setdefault("messages", []).append(record)
            if stage == STAGE_REVIEW:
                for item in task.get("part_b") or []:
                    if item.get("member_id") == member_id:
                        item["status"] = MEMBER_IN_PROGRESS
                        # 上一轮的意见与原始输出先清掉：这一轮还没出结果，
                        # 留着旧内容会让人把上一轮的意见当成本轮的。
                        # 但本轮的 Prompt 已经拼好、进程马上要起，先落下来：
                        # 否则环节进行中查到的 Prompt 是空的，UI 只能显示「未留存」。
                        item.update({
                            "summary": "", "raw_output": "",
                            "sent_prompt": prompt, "msg_id": record["id"],
                            "elapsed": 0,
                            # started_at 必须现在写：卡片上「进行中」这一段要显示开始时刻
                            # 并实时计时，等跑完才写就整段时间都是空的
                            "started_at": int(time.time()), "finished_at": None,
                            "review_verdict": "", "review_counts": {},
                        })
            elif stage == STAGE_EXEC:
                # 重跑执行时先清空上一轮的简报与产物：本轮失败也不该拿旧简报当结果展示；
                # Prompt 同理换成本轮的（清成空会让「执行中」这段时间看起来没留存）
                task["part_a"].update({
                    "status": MEMBER_IN_PROGRESS, "brief": "", "artifacts": [],
                    "sent_prompt": prompt, "raw_output": "", "msg_id": record["id"],
                    "elapsed": 0, "started_at": int(time.time()), "finished_at": None,
                })
                task["artifacts"] = []
                # 单任务：把本轮 Prompt 同步到聊天可见的结果消息，
                # 执行进行中（点 ℹ️）就能看到「发送给 AI 的 Prompt」，不用等跑完；
                # 同时记下 started_at，让结果消息顶部实时计时、显示「正在思考中」
                if task.get("result_msg_id"):
                    self._update_message(session, task["result_msg_id"], {
                        "sent_prompt": prompt,
                        "started_at": int(time.time()),
                        "status": STATUS_RUNNING,
                    })
                # 主创重跑意味着上一轮的审核意见全部作废：不清掉的话，卡片上会挂着
                # 上一轮的「已完成」和旧意见，看起来像这一轮审核已经通过了
                for item in task.get("part_b") or []:
                    item.update({
                        "status": MEMBER_WAITING, "summary": "", "raw_output": "",
                        "sent_prompt": "", "msg_id": "", "elapsed": 0,
                        "review_verdict": "", "review_counts": {},
                    })
            elif stage == STAGE_DELIVER:
                task["part_a"]["final_status"] = MEMBER_IN_PROGRESS
                # 交付的 Prompt 同样先落下来，交付进行中也能查；开始时刻现在写，
                # 卡片上「交付中」这一段才能显示开始时间并实时计时
                task["part_a"].update({
                    "final": {}, "final_sent_prompt": prompt, "final_raw_output": "",
                    "final_elapsed": 0,
                    "final_started_at": int(time.time()), "final_finished_at": None,
                })
            task.setdefault("attempts", {})[attempt_key] = task["attempts"].get(attempt_key, 0) + 1
            self._update_card(session, task)
            self._save(session)

        with self._lock:
            self._active_keys[key] = {"sid": sid, "tid": tid, "stage": stage, "member_id": member_id}

        runner_emp = dict(emp)
        if read_only:
            runner_emp["write_access"] = False  # 审核环节一律只读

        # 流式：执行器每吐一段就写进暂存区，界面按 100ms 级轮询取尾部渲染，
        # 等待期间能看到「正在写什么」，而不是只有一个「正在思考中」。
        # 暂存用的 key 是 `{tid}:{stage}:{member_id}`（会话另记），与前端拼的完全一致。
        live_key = streaming.stream_key(tid, stage, emp_id or "")
        sink = self._stream_sink(sid, tid, stage, emp_id or "", live_key) if self._streams else None
        run_kwargs: Dict[str, Any] = {"session_id": key, "workspace_dir": workspace_dir}
        if sink:
            run_kwargs["on_chunk"] = sink

        started = time.time()
        cancelled_flag = False
        try:
            res = self.runner(runner_emp, prompt, **run_kwargs)
            # 语义层重试（仅审核环节）：模型只回了「我先读取…」这类开场白、没给任何结论时，
            # 带强指令再问一次。审核是只读的，重跑不会重复写盘；复用同一个执行单元与记录，
            # 不会多出一条 task_record，也不额外占队列。仍然失败则交给「无意见」兜底。
            if stage == STAGE_REVIEW and key not in CANCELLED_KEYS and _review_needs_retry(res):
                print(f"[TaskManager] {emp.get('name') or emp_id} 审核疑似未给出结论，重试一次")
                # 上一轮那句开场白先清掉：重试期间界面上不该还挂着旧文本
                if sink:
                    streaming.clear(live_key)
                try:
                    res_retry = self.runner(runner_emp, prompt + REVIEW_RETRY_HINT, **run_kwargs)
                except Exception:
                    res_retry = None

                if res_retry is not None and key not in CANCELLED_KEYS:
                    res_retry.elapsed = round(
                        (getattr(res_retry, "elapsed", 0) or 0)
                        + (getattr(res, "elapsed", 0) or 0),
                        2,
                    )
                    res_retry.raw_output = (
                        f"{getattr(res, 'raw_output', '')}\n--- retry ---\n"
                        f"{getattr(res_retry, 'raw_output', '')}"
                    )
                    res = res_retry
        except Exception as exc:
            res = type("FallbackResult", (), {
                "status": "error", "output": str(exc), "raw_output": str(exc),
                "elapsed": 0.0,
            })()
        finally:
            with self._lock:
                self._active_keys.pop(key, None)
                # 取消标记只对「这一次执行」有效：早期实现把它留在进程级集合里，
                # 同一个 key 下一轮执行会被无条件判成已取消（真的调了模型却不认结果）
                cancelled_flag = key in CANCELLED_KEYS
                CANCELLED_KEYS.discard(key)
            # 这一轮已结束：暂存的半成品立刻撤掉，结果马上会以正式记录落库。
            # 不清理的话，界面会一直显示上一轮的「正在写」文本
            if sink:
                streaming.clear(live_key)

        status = str(getattr(res, "status", "") or "")
        raw = str(getattr(res, "raw_output", "") or "") or str(getattr(res, "output", "") or "")
        parsed = parse_output(str(getattr(res, "output", "") or ""))
        elapsed = round(time.time() - started, 2)
        started_ts, finished_ts = int(started), int(time.time())

        if status in ("interrupted",) or cancelled_flag:
            mapped = "cancelled"
        elif status in ("timeout",):
            mapped = STATUS_TIMEOUT
        elif status in ("error", "failed"):
            mapped = STATUS_FAILED
        else:
            # 执行记录代表「这一次执行完成」，不是整个任务已交付；任务终态由各环节写回
            mapped = STATUS_DONE

        with self.store.session_lock(sid):
            session = self._get(sid)
            if session:
                self._update_message(session, record["id"], {
                    "content": parsed["summary"],
                    "raw_response": raw,
                    "status": mapped,
                    "elapsed_time": elapsed,
                    "started_at": started_ts,
                    "finished_at": finished_ts,
                })
                self._save(session)

        return {
            "status": mapped,
            "cancelled": mapped == "cancelled",
            "summary": parsed["summary"],
            "artifacts": parsed["artifacts"],
            "changes": parsed["changes"],
            "review": parsed["review"],
            "raw": raw,
            "prompt": prompt,
            "record_msg_id": record["id"],
            "elapsed": elapsed,
            # 起止时刻随结果一起回传：三段各自落库，卡片才能显示「几点开始、几点返回」
            "started_at": started_ts,
            "finished_at": finished_ts,
        }

    @staticmethod
    def _update_message(session: Dict[str, Any], msg_id_value: Optional[str], patch: Dict[str, Any]) -> None:
        if not msg_id_value:
            return
        for msg in session.get("messages") or []:
            if isinstance(msg, dict) and msg.get("id") == msg_id_value:
                msg.update(patch)
                return

    @staticmethod
    def _state_label(status: str) -> str:
        return {
            STATUS_DELIVERED: "完成", STATUS_DONE: "完成", STATUS_FAILED: "失败",
            STATUS_CANCELLED: "已取消", STATUS_TIMEOUT: "超时", STATUS_NO_OPINION: "无意见",
            STATUS_RUNNING: "进行中", STATUS_QUEUED: "排队中",
        }.get(status, status)

    @staticmethod
    def _stage_label(stage: str) -> str:
        """流水文案用的环节名：别让用户看到 exec / review / deliver 这种内部值。"""
        return {
            STAGE_EXEC: "执行", STAGE_REVIEW: "审核", STAGE_DELIVER: "交付",
        }.get(stage, stage)

    # ---------------- 取消 / 重试 ----------------
    # 可取消的环节状态：还没开始（waiting）、正在跑（in_progress / running）
    CANCELLABLE_STAGE_STATES = (MEMBER_WAITING, MEMBER_IN_PROGRESS, STATUS_RUNNING)

    def cancel(self, sid: str, tid: str, stage: str, member_id: str = "") -> Dict[str, Any]:
        """静默取消：终止进程、状态转已取消、不追加系统消息、不弹窗。

        只受理「还没结束的任务 + 还没结束的环节」：终态任务不可逆转，
        已经跑完的环节也不能被打回已取消（那会让卡片与磁盘产物互相矛盾）。
        """
        with self.store.session_lock(sid):
            session = self._get(sid)
            if not session:
                return {"error": "会话不存在"}
            task = (session.get("tasks") or {}).get(tid)
            if not task:
                return {"error": "任务不存在"}
            if task.get("status") in FINAL_TASK_STATES:
                return {
                    "error": f"任务已{self._state_label(task.get('status'))}，不能取消",
                    "code": "task_finished",
                }
            part_a = task.get("part_a") or {}
            if stage == STAGE_REVIEW:
                item = next(
                    (i for i in task.get("part_b") or [] if i.get("member_id") == member_id),
                    None,
                )
                if not item:
                    return {"error": "该成员不在这次协助审核里", "code": "stage_not_active"}
                if item.get("status") not in self.CANCELLABLE_STAGE_STATES:
                    return {"error": "这位成员的审核已结束，不能取消", "code": "stage_not_active"}
            elif stage == STAGE_DELIVER:
                if part_a.get("final_status") not in self.CANCELLABLE_STAGE_STATES:
                    return {"error": "交付环节已结束，不能取消", "code": "stage_not_active"}
            else:
                if part_a.get("status") not in self.CANCELLABLE_STAGE_STATES:
                    return {"error": "执行环节已结束，不能取消", "code": "stage_not_active"}
            emp_id = member_id or task.get("owner_id") or ""
            key = execution_key(sid, tid, stage, emp_id)
            if task.get("status") != STATUS_QUEUED:
                # 排队中的任务根本没有进程可杀，也不会再进入执行单元（已从队列摘除且状态转已取消），
                # 写取消标记没人消费，只会在进程级集合里长期留一个字符串
                CANCELLED_KEYS.add(key)
            if stage == STAGE_EXEC:
                task["part_a"]["status"] = STATUS_CANCELLED
            elif stage == STAGE_DELIVER:
                task["part_a"]["final_status"] = STATUS_CANCELLED
            elif stage == STAGE_REVIEW:
                cancelled_name = ""
                for item in task.get("part_b") or []:
                    if item.get("member_id") == member_id:
                        item["status"] = STATUS_CANCELLED
                        cancelled_name = str(item.get("name") or "")
                # 只取消这位成员的审核；主创仍按已有意见交付（文档 4.3）
                self._add_flow(
                    task, f"已取消 {cancelled_name or member_id} 的审核，主创继续交付"
                )
                self._update_card(session, task)
                self._save(session)
                try:
                    self.killer(key)
                except Exception as exc:
                    print(f"[TaskManager] 终止进程失败: {exc}")
                return {"ok": True, "status": STATUS_CANCELLED}
            task["status"] = STATUS_CANCELLED
            task["finished_at"] = int(time.time())
            self._add_flow(task, "已取消")
            # 单任务的结果消息要跟着转「已取消」：排队中被取消的任务不会再被执行，
            # 不在这里同步，消息就会永远停在「排队中」且带一个点不动的取消按钮
            if task.get("result_msg_id"):
                msg = next(
                    (m for m in session.get("messages") or []
                     if isinstance(m, dict) and m.get("id") == task["result_msg_id"]),
                    None,
                )
                if msg and msg.get("status") in (
                    STATUS_QUEUED, STATUS_RUNNING, MEMBER_WAITING, MEMBER_IN_PROGRESS,
                ):
                    patch: Dict[str, Any] = {"status": STATUS_CANCELLED}
                    if not str(msg.get("content") or "").strip():
                        patch["content"] = "已取消（未执行）"
                    self._update_message(session, task["result_msg_id"], patch)
            # 已取消的任务不再占队列位置
            owner_queue = session.get("task_queues", {}).get(task.get("owner_id")) or []
            if tid in owner_queue:
                owner_queue.remove(tid)
            self._update_card(session, task)
            self._save(session)

        try:
            self.killer(key)  # 默认实现：SIGTERM，0.3s 内不退则 SIGKILL
        except Exception as exc:
            print(f"[TaskManager] 终止进程失败: {exc}")

        return {"ok": True, "status": STATUS_CANCELLED}

    def retry(self, sid: str, tid: str, stage: str, member_id: str = "") -> Dict[str, Any]:
        """重跑被点击的环节，不新增任务、不增加流程轮次。"""
        with self.store.session_lock(sid):
            session = self._get(sid)
            if not session:
                return {"error": "会话不存在"}
            task = (session.get("tasks") or {}).get(tid)
            if not task:
                return {"error": "任务不存在"}
            redo = False
            needs_deliver = False
            if stage == STAGE_EXEC:
                current = task["part_a"].get("status")
                # 执行环节有两种入口：失败 / 超时 / 已取消是「重试」，已成功是「重新生成」。
                # 单任务重跑执行就是把这条回答重写一遍；协助任务重跑执行等于整轮重来
                # （产物重做、审核重审、交付重出），卡片上三个环节各有自己的入口。
                redo = current in (STATUS_DONE, STATUS_DELIVERED)
                allowed = redo or current in RETRYABLE_STATES or task.get("status") in RETRYABLE_STATES
            elif stage == STAGE_DELIVER:
                part_a = task.get("part_a") or {}
                current = part_a.get("final_status")
                # 交付只能发生在「执行成功」之后：执行失败还允许重跑交付，
                # 会产出一个没有产物、没有审核意见却显示「已交付」的任务
                executed = part_a.get("status") == STATUS_DONE
                allowed = executed and (
                    current in (STATUS_DONE, *RETRYABLE_STATES)
                    or task.get("status") in (STATUS_DELIVERED, *RETRYABLE_STATES)
                )
            else:
                current = next(
                    (item.get("status") for item in task.get("part_b") or []
                     if item.get("member_id") == member_id), None
                )
                # 审核已成功也可以重跑：合同上的意见要能重出，不能只有失败时才放行
                redo = current == STATUS_DONE
                allowed = redo or current in RETRYABLE_STATES
                # 终稿是否过期，等这次审核真的跑出结论再判（见 _retry_worker）：
                # 点击的瞬间还不知道会成功还是失败，先标过期会误伤没变的终稿
            if not allowed:
                return {"error": "当前环节不需要重试"}
            emp_id = member_id or task.get("owner_id") or ""
            CANCELLED_KEYS.discard(execution_key(sid, tid, stage, emp_id))
            if stage in (STAGE_EXEC, STAGE_DELIVER):
                # 重跑执行=这一轮从头推进；重跑交付=重新生成终版
                task["status"] = STATUS_RUNNING
                task["finished_at"] = None
                # 头部计时从这一轮重新算：不清掉的话「重新生成」会接着上一轮的
                # started_at 一直计下去（看起来像跑了半小时）
                task["started_at"] = int(time.time())
                if stage == STAGE_EXEC and task.get("type") == TYPE_SINGLE and task.get("result_msg_id"):
                    # 单任务重跑期间消息转「执行中」，避免还挂着失效的「重试」按钮
                    self._update_message(session, task["result_msg_id"], {"status": STATUS_RUNNING})
            if stage == STAGE_EXEC:
                # 重跑主创执行：之前的审核与交付作废，从审核环节重新推进
                task["part_a"]["status"] = MEMBER_WAITING
                # 上一轮的简报与耗时一并清掉，否则刚点下去卡片上还挂着旧结论
                task["part_a"]["brief"] = ""
                task["part_a"]["elapsed"] = 0
                task["part_a"]["started_at"] = None
                task["part_a"]["finished_at"] = None
                task["part_a"]["final"] = {}
                task["part_a"]["final_status"] = MEMBER_WAITING
                task["part_a"]["final_elapsed"] = 0
                task["part_a"]["final_started_at"] = None
                task["part_a"]["final_finished_at"] = None
                for item in task.get("part_b") or []:
                    item["status"] = MEMBER_WAITING
                    item["summary"] = ""
                    item["raw_output"] = ""
                    item["sent_prompt"] = ""
                    item["msg_id"] = ""
                    item["review_verdict"] = ""
                    item["review_counts"] = {}
                    item["elapsed"] = 0
                    item["started_at"] = None
                    item["finished_at"] = None
                task.pop("deliver_stale", None)
                # 整轮重来：上一版终稿的来源标签作废，这次交付是正常流程里的
                for key in ("deliver_source", "deliver_source_at"):
                    task.pop(key, None)
                task["part_a"]["final_source"] = ""
                task["step"] = 1
            elif stage == STAGE_REVIEW:
                # 重跑这一位审核：卡片回到「审核中」，旧意见先清掉，
                # 否则还会显示上一轮的结论，看起来像没生效
                task["status"] = STATUS_RUNNING
                task["finished_at"] = None
                task["step"] = 2
                # 单条重审同样算一轮：头部「开始 / 计时」从点下去的那一刻算，
                # 否则会沿用整条任务最早的 started_at
                task["started_at"] = int(time.time())
                # 一次「重新审核」算一轮。多位审核并发重跑时只补一次交付：
                # 谁最后跑完，看 generation 比 auto_deliver_generation 大就补一次。
                task["review_generation"] = int(task.get("review_generation") or 0) + 1
                for item in task.get("part_b") or []:
                    if item.get("member_id") == member_id:
                        item["status"] = MEMBER_WAITING
                        item["summary"] = ""
                        item["raw_output"] = ""
                        # 旧输入快照里还是上一版规程，重审期间不能继续挂在这一段上：
                        # 新 Prompt 会在 _execute_unit 里落库；若成员占用失败而没跑起来，
                        # 也该显示「暂无输入」，而不是让人误以为新规程没生效。
                        item["sent_prompt"] = ""
                        item["msg_id"] = ""
                        item["review_verdict"] = ""
                        item["review_counts"] = {}
                        item["elapsed"] = 0
                        item["started_at"] = None
                        item["finished_at"] = None
            self._add_flow(task, f"{'重新生成' if redo else '重试'}{self._stage_label(stage)}")
            self._update_card(session, task)
            self._save(session)

        retry_key = execution_key(sid, tid, stage, member_id or task.get("owner_id") or "")
        with self._lock:
            if retry_key in self._retries:
                return {"error": "该环节正在重试中"}
            self._retries.add(retry_key)

        thread = threading.Thread(
            target=self._retry_worker_guarded,
            args=(sid, tid, stage, member_id, retry_key),
            name=f"task-retry-{tid}-{stage}", daemon=True,
        )
        thread.start()
        return {"ok": True}

    def _retry_worker_guarded(self, sid: str, tid: str, stage: str, member_id: str, retry_key: str) -> None:
        try:
            self._retry_worker(sid, tid, stage, member_id)
        except Exception as exc:
            print(f"[TaskManager] 重试 {stage} 失败: {exc}")
        finally:
            with self._lock:
                self._retries.discard(retry_key)

    def _retry_worker(self, sid: str, tid: str, stage: str, member_id: str) -> None:
        settings = self.store.load_settings()
        with self.store.session_lock(sid):
            session = self._get(sid)
            if not session:
                return
            task = (session.get("tasks") or {}).get(tid) or {}
            workspace_dir = session.get("workspace_dir", "")
            task_type = task.get("type")

        if stage == STAGE_EXEC:
            # 单任务重跑的还是单任务：走协助任务流水线会把结果消息漏掉，还会多跑一次交付。
            # 占用交给 _execute：这里再占用会让审核阶段一直握着主创，交叉协作会成环
            if task_type == TYPE_SINGLE:
                self._run_single(sid, tid, settings, workspace_dir)
            else:
                ok = self._run_exec(sid, tid, settings, workspace_dir)
                if ok:
                    self._run_reviews(sid, tid, settings, workspace_dir)
                    self._run_deliver(sid, tid, settings, workspace_dir)
        elif stage == STAGE_REVIEW:
            # _run_review_one 内部的 _execute 会占用该成员；这里再占用一次会等自己
            self._run_review_one(sid, tid, member_id, settings, workspace_dir)
            # 所有审核都出了结论 → 自动补一次交付（用户认可这个行为），并把这版终稿
            # 标成「审核补全后重新生成」，和用户手点的交付区分开。
            # 用 review_generation 去重：两位审核并发重跑时只补一次，不会因为先后
            # 返回就重出两份终稿；用户之后又点一次「重新审核」则是新一轮，再补一次。
            auto = False
            with self.store.session_lock(sid):
                session = self._get(sid)
                task = ((session or {}).get("tasks") or {}).get(tid) or {}
                if task:
                    part_a = task.get("part_a") or {}
                    reviewers = task.get("part_b") or []
                    finished_states = (
                        STATUS_DONE, STATUS_FAILED, STATUS_TIMEOUT,
                        STATUS_CANCELLED, STATUS_NO_OPINION,
                    )
                    conclusion_states = (STATUS_DONE, STATUS_NO_OPINION)
                    exec_ok = part_a.get("status") == STATUS_DONE
                    final_ok = part_a.get("final_status") == STATUS_DONE
                    all_settled = bool(reviewers) and all(
                        (i.get("status") or "") in finished_states for i in reviewers
                    )
                    all_concluded = bool(reviewers) and all(
                        (i.get("status") or "") in conclusion_states for i in reviewers
                    )
                    generation = int(task.get("review_generation") or 0)
                    auto_done = int(task.get("auto_deliver_generation") or 0)
                    # 还有新审核结论没被任何一次交付消费掉
                    pending_reviews = exec_ok and generation > auto_done
                    needs_auto = (
                        pending_reviews and all_concluded
                        and task.get("status") != STATUS_CANCELLED
                    )
                    already_running = bool(task.get("auto_deliver_inflight"))
                    if needs_auto and not already_running:
                        # 在锁内先认领，另一个并发的重审线程看到 generation 已被消费就不会再补
                        task["auto_deliver_generation"] = generation
                        task["auto_deliver_inflight"] = True
                        task["deliver_stale"] = True
                        task["status"] = STATUS_RUNNING
                        task["finished_at"] = None
                        task["step"] = 3
                        self._add_flow(task, "全部审核已出结论，自动补一次交付")
                        auto = True
                    elif not already_running and all_settled:
                        # 审核没齐（有失败/超时/被取消）或这一轮已经补过：只收尾。
                        # 终稿过期时留提示，是否重出由用户点「重新生成交付」决定。
                        member_ok = next(
                            (i.get("status") for i in reviewers
                             if i.get("member_id") == member_id), ""
                        )
                        # 只有「新结论还没被自动交付消费」时才算过期；
                        # 补完交付后另一个并发重审线程回来收尾，不该又标一次过期
                        if final_ok and member_ok in conclusion_states and pending_reviews:
                            task["deliver_stale"] = True
                        was_running = task.get("status") == STATUS_RUNNING
                        task["status"] = STATUS_DELIVERED if final_ok else STATUS_DONE
                        task["finished_at"] = int(time.time())
                        if final_ok and task.get("deliver_stale") and was_running:
                            self._add_flow(task, "审核意见已更新，终稿待重新生成")
                    self._update_card(session, task)
                    self._save(session)
            if auto:
                try:
                    self._run_deliver(
                        sid, tid, settings, workspace_dir, source="review_auto"
                    )
                finally:
                    self._clear_auto_deliver_inflight(sid, tid)
        elif stage == STAGE_DELIVER:
            # 用户手点「重新生成交付」：来源标签换成 manual，不再挂自动补全的标记
            self._run_deliver(sid, tid, settings, workspace_dir, source="manual")

    def _clear_auto_deliver_inflight(self, sid: str, tid: str) -> None:
        """自动补交付异常退出时兜底解锁，避免这一轮之后再也不补交付。"""
        with self.store.session_lock(sid):
            session = self._get(sid)
            task = ((session or {}).get("tasks") or {}).get(tid)
            if task and task.get("auto_deliver_inflight"):
                task.pop("auto_deliver_inflight", None)
                self._update_card(session, task)
                self._save(session)

    # ---------------- 服务重启恢复 ----------------
    def recover_on_startup(self) -> int:
        """服务重启后：进行中的任务标记已取消，排队任务重新拉起 worker。"""
        recovered = 0
        try:
            summaries = self.store.list_sessions()
        except Exception:
            return 0
        for summary in summaries or []:
            sid = summary.get("id") if isinstance(summary, dict) else None
            if not sid:
                continue
            with self.store.session_lock(sid):
                session = self._get(sid)
                if not session:
                    continue
                ensure_task_fields(session)
                changed = False
                for task in (session.get("tasks") or {}).values():
                    if not isinstance(task, dict):
                        continue
                    if task.pop("auto_deliver_inflight", None):
                        # 服务重启时那次自动补交付的进程已经没了，解锁留着的占用
                        changed = True
                    if task.get("status") in FINAL_TASK_STATES:
                        # 已经结束的任务不再动：早期实现会把已交付单任务的 part_a 从
                        # in_progress 改成 cancelled，还追加一条「进行中的环节已取消」的假流水
                        continue
                    # 不只把任务打成已取消：三个环节的「进行中」标记也要一起收掉，
                    # 否则卡片上那些位置既没有取消（进程已没了）也没有重试（状态不可重试），用户被卡死
                    touched = False
                    part_a = task.get("part_a")
                    if isinstance(part_a, dict):
                        for field in ("status", "final_status"):
                            if part_a.get(field) in (MEMBER_IN_PROGRESS, STATUS_RUNNING):
                                part_a[field] = STATUS_CANCELLED
                                touched = True
                    for item in task.get("part_b") or []:
                        if isinstance(item, dict) and item.get("status") in (MEMBER_IN_PROGRESS, STATUS_RUNNING):
                            item["status"] = STATUS_CANCELLED
                            touched = True
                    if task.get("status") == STATUS_RUNNING:
                        task["status"] = STATUS_CANCELLED
                        task["finished_at"] = int(time.time())
                        recovered += 1
                        touched = True
                    if touched:
                        self._add_flow(task, "服务重启，进行中的环节已取消，可重试")
                        self._update_card(session, task)
                        changed = True
                if changed:
                    self._save(session)
                queued_members = set()
                for tid, task in (session.get("tasks") or {}).items():
                    if isinstance(task, dict) and task.get("status") == STATUS_QUEUED:
                        queued_members.add(task.get("owner_id"))
            for member_id in queued_members:
                if member_id:
                    self.ensure_worker(sid, member_id)
        return recovered


# 取消标记是进程级的：runner 返回 interrupted 时用于区分「用户取消」与「进程自己非零退出」
CANCELLED_KEYS: set = set()


# 全局默认实例（app.py 使用；测试可自行 new 一个注入假 runner）
task_manager = TaskManager()
