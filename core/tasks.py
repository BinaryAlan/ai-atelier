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
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import core.storage as storage
from core.executor import Executor

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

FINAL_TASK_STATES = (STATUS_DELIVERED, STATUS_CANCELLED, STATUS_FAILED)
RETRYABLE_STATES = (STATUS_FAILED, STATUS_CANCELLED, STATUS_TIMEOUT, STATUS_NO_OPINION)


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


def member_aliases(emp: Dict[str, Any]) -> List[str]:
    """成员可被 @ 的别名：全名、id、以及全名括号里的短名（如 DS4 / HY4）。"""
    name = str(emp.get("name") or "")
    short = re.findall(r"[（(]([^（）()]+)[）)]", name)
    return [a for a in [name, str(emp.get("id") or "")] + short if a]


def _match_mention(text: str, settings: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], int]:
    """匹配文本开头的一个 @成员（名字或 id），支持含空格的名字，返回 (成员, 消费长度)。"""
    if not text.startswith("@"):
        return None, 0
    tokens = []
    for emp in _member_candidates(settings):
        for alias in member_aliases(emp):
            tokens.append((alias, emp))
    # 长别名优先，避免 "agy" 抢先匹配 "agy-pro"
    tokens.sort(key=lambda item: len(item[0]), reverse=True)
    for alias, emp in tokens:
        if text.startswith("@" + alias):
            return emp, len(alias) + 1
    return None, 0


def parse_collab(content: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    """严格解析 /collab @主创 @协助成员... 任务内容。

    返回 {"ok": bool, "error": str, "owner_id", "reviewer_ids", "goal"}。
    缺少主创或缺少协助成员时一律拦截，不创建任务。
    """
    result: Dict[str, Any] = {
        "ok": False, "error": "", "owner_id": "", "reviewer_ids": [], "goal": ""
    }
    rest = re.sub(r"^/collab\s*", "", content or "", count=1, flags=re.IGNORECASE).lstrip()
    if not rest:
        result["error"] = "缺少主创和协助成员：格式 /collab @主创 @协助成员 任务内容"
        return result

    owner_id = ""
    reviewer_ids: List[str] = []
    pos = 0
    while True:
        tail = rest[pos:].lstrip()
        if not tail.startswith("@"):
            break
        consumed_ws = len(rest[pos:]) - len(rest[pos:].lstrip())
        emp, used = _match_mention(tail, settings)
        if not emp:
            break
        if not owner_id:
            owner_id = str(emp["id"])
        elif str(emp["id"]) not in reviewer_ids and str(emp["id"]) != owner_id:
            reviewer_ids.append(str(emp["id"]))
        pos += consumed_ws + used

    goal = rest[pos:].strip()

    if not owner_id:
        result["error"] = "缺少主创：请用 /collab @主创 @协助成员 任务内容"
        return result
    if not reviewer_ids:
        result["error"] = "缺少协助成员：主创之后至少再指定一位协助成员"
        return result
    if not goal:
        result["error"] = "缺少任务内容：/collab @主创 @协助成员 之后要写要做什么"
        return result

    result.update(ok=True, owner_id=owner_id, reviewer_ids=reviewer_ids, goal=goal)
    return result


def parse_single(content: str, settings: Dict[str, Any]) -> Optional[str]:
    """普通消息里找一位被 @ 的成员，返回成员 id；没有点名返回 None。"""
    aliases: List[Tuple[str, Dict[str, Any]]] = []
    for emp in _member_candidates(settings):
        for alias in member_aliases(emp):
            aliases.append((alias, emp))
    aliases.sort(key=lambda pair: len(pair[0]), reverse=True)
    for alias, emp in aliases:
        if ("@" + alias) in content:
            return str(emp["id"])
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
        lines = [f"- {o.get('name') or o.get('member_id')}: {o.get('summary') or ''}" for o in opinions]
        parts.append("【审核意见】\n" + "\n".join(lines))
    parts.append(
        "【输出要求】请优先输出一个 JSON 对象，字段为 "
        '{"summary": "简短说明", "artifacts": [{"path": "...", "summary": "...", "bytes": 0}]}；'
        "无法给出 JSON 时输出纯文本，软件会按纯文本展示。"
    )
    return "\n\n".join(p for p in parts if p)


def parse_output(text: str) -> Dict[str, Any]:
    """解析结构化输出；解析不到时按纯文本展示，不影响交付。"""
    raw = text or ""
    candidates = [raw.strip()]
    for block in re.findall(r"```(?:json)?\s*(.*?)```", raw, flags=re.S | re.I):
        candidates.insert(0, block.strip())
    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                artifacts = data.get("artifacts")
                if not isinstance(artifacts, list):
                    artifacts = []
                clean = []
                for item in artifacts:
                    if isinstance(item, dict) and item.get("path"):
                        clean.append({
                            "path": str(item["path"]),
                            "summary": str(item.get("summary") or ""),
                            "bytes": int(item.get("bytes") or 0),
                        })
                return {
                    "summary": str(data.get("summary") or "").strip() or raw.strip(),
                    "artifacts": clean,
                    "changes": str(data.get("changes") or data.get("opinion_handling") or "").strip(),
                }
        except (ValueError, TypeError):
            continue
    return {"summary": raw.strip(), "artifacts": [], "changes": ""}


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
        "flow": list(task.get("flow") or []),
        "artifacts": list(task.get("artifacts") or []),
        "part_a": {
            "brief": part_a.get("brief", ""),
            "artifacts": list(part_a.get("artifacts") or []),
            "final": dict(part_a.get("final") or {}),
            "status": part_a.get("status", MEMBER_WAITING),
            "msg_id": part_a.get("msg_id"),
            "sent_prompt": part_a.get("sent_prompt", ""),
            "raw_output": part_a.get("raw_output", ""),
            "final_status": part_a.get("final_status", MEMBER_WAITING),
            "final_sent_prompt": part_a.get("final_sent_prompt", ""),
            "final_raw_output": part_a.get("final_raw_output", ""),
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
                # 没有点名成员：只作为上下文保留，不创建任务
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
        for emp in settings.get("employees") or []:
            if isinstance(emp, dict) and str(emp.get("id")) == str(emp_id):
                if emp.get("enabled", True) and emp.get("cli_cmd"):
                    return emp
        return None

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
                        self._add_flow(task, f"{member_id} 开始执行")
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

    # 同一成员最多等这么久；超时则照常执行（宁可少见地不串行，也不要两个任务互相等成死锁）
    MEMBER_WAIT_SECONDS = 1800

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
                    print(f"[TaskManager] 等 {member_id} 空闲超时，本次不执行")
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
            self._add_flow(task, f"单任务{self._state_label(status)}")
            self._update_message(session, task.get("result_msg_id"), {
                "content": result.get("summary", ""),
                "status": status,
                "sent_prompt": result.get("prompt", ""),
                "raw_response": result.get("raw", ""),
                "artifacts": task["artifacts"],
                "elapsed_time": result.get("elapsed", 0),
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
                self._add_flow(task, "主创执行已取消")
            elif result.get("status") in (STATUS_FAILED, STATUS_TIMEOUT):
                part_a["status"] = result["status"]
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
                elif not (result.get("summary") or "").strip():
                    item["status"] = STATUS_NO_OPINION
                else:
                    item["status"] = STATUS_DONE
                item.update({
                    "summary": result.get("summary", ""),
                    "raw_output": result.get("raw", ""),
                    "sent_prompt": result.get("prompt", ""),
                    "msg_id": result.get("record_msg_id", ""),
                    "elapsed": result.get("elapsed", 0),
                })
                self._add_flow(task, f"{item.get('name')} 审核{self._state_label(item['status'])}")
                break
            self._update_card(session, task)
            self._save(session)

    def _run_deliver(self, sid: str, tid: str, settings: Dict[str, Any], workspace_dir: str) -> None:
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
            elif result.get("status") in (STATUS_FAILED, STATUS_TIMEOUT):
                task["status"] = result["status"]
                part_a["final_status"] = result["status"]
            else:
                task["artifacts"] = scan_artifacts(
                    workspace_dir, result.get("artifacts") or task.get("artifacts") or []
                )
                part_a["artifacts"] = task["artifacts"]
                part_a["final"] = {
                    "summary": result.get("summary", ""),
                    "opinion_handling": result.get("changes", ""),
                    "changed_files": [a.get("path") for a in task["artifacts"]],
                }
                part_a["final_status"] = STATUS_DONE
                part_a["final_sent_prompt"] = result.get("prompt", "")
                part_a["final_raw_output"] = result.get("raw", "")
                task["status"] = STATUS_DELIVERED
                task["step"] = 3
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
            elif stage == STAGE_EXEC:
                task["part_a"]["status"] = MEMBER_IN_PROGRESS
            elif stage == STAGE_DELIVER:
                task["part_a"]["final_status"] = MEMBER_IN_PROGRESS
            task.setdefault("attempts", {})[attempt_key] = task["attempts"].get(attempt_key, 0) + 1
            self._update_card(session, task)
            self._save(session)

        with self._lock:
            self._active_keys[key] = {"sid": sid, "tid": tid, "stage": stage, "member_id": member_id}

        runner_emp = dict(emp)
        if read_only:
            runner_emp["write_access"] = False  # 审核环节一律只读

        started = time.time()
        cancelled_flag = False
        try:
            res = self.runner(
                runner_emp,
                prompt,
                session_id=key,
                workspace_dir=workspace_dir,
            )
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

        status = str(getattr(res, "status", "") or "")
        raw = str(getattr(res, "raw_output", "") or "") or str(getattr(res, "output", "") or "")
        parsed = parse_output(str(getattr(res, "output", "") or ""))
        elapsed = round(time.time() - started, 2)

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
                })
                self._save(session)

        return {
            "status": mapped,
            "cancelled": mapped == "cancelled",
            "summary": parsed["summary"],
            "artifacts": parsed["artifacts"],
            "changes": parsed["changes"],
            "raw": raw,
            "prompt": prompt,
            "record_msg_id": record["id"],
            "elapsed": elapsed,
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
            CANCELLED_KEYS.add(key)
            if stage == STAGE_EXEC:
                task["part_a"]["status"] = STATUS_CANCELLED
            elif stage == STAGE_DELIVER:
                task["part_a"]["final_status"] = STATUS_CANCELLED
            elif stage == STAGE_REVIEW:
                for item in task.get("part_b") or []:
                    if item.get("member_id") == member_id:
                        item["status"] = STATUS_CANCELLED
                # 只取消这位成员的审核；主创仍按已有意见交付（文档 4.3）
                self._add_flow(task, f"已取消 {member_id} 的审核，主创继续交付")
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
            if stage == STAGE_EXEC:
                current = task["part_a"].get("status")
                # 执行：只在失败 / 超时 / 已取消后提供重试
                allowed = current in RETRYABLE_STATES or task.get("status") in RETRYABLE_STATES
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
                allowed = current in RETRYABLE_STATES
            if not allowed:
                return {"error": "当前环节不需要重试"}
            emp_id = member_id or task.get("owner_id") or ""
            CANCELLED_KEYS.discard(execution_key(sid, tid, stage, emp_id))
            if stage in (STAGE_EXEC, STAGE_DELIVER):
                # 重跑执行=这一轮从头推进；重跑交付=重新生成终版
                task["status"] = STATUS_RUNNING
                task["finished_at"] = None
                if stage == STAGE_EXEC and task.get("type") == TYPE_SINGLE and task.get("result_msg_id"):
                    # 单任务重跑期间消息转「执行中」，避免还挂着失效的「重试」按钮
                    self._update_message(session, task["result_msg_id"], {"status": STATUS_RUNNING})
            if stage == STAGE_EXEC:
                # 重跑主创执行：之前的审核与交付作废，从审核环节重新推进
                task["part_a"]["status"] = MEMBER_WAITING
                task["part_a"]["final"] = {}
                task["part_a"]["final_status"] = MEMBER_WAITING
                for item in task.get("part_b") or []:
                    item["status"] = MEMBER_WAITING
                    item["summary"] = ""
                task["step"] = 1
            self._add_flow(task, f"重试 {stage}")
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
            with self.store.session_lock(sid):
                session = self._get(sid)
                if session and tid in (session.get("tasks") or {}):
                    self._update_card(session, session["tasks"][tid])
                    self._save(session)
        elif stage == STAGE_DELIVER:
            self._run_deliver(sid, tid, settings, workspace_dir)

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
