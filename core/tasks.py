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

FINAL_TASK_STATES = (STATUS_DELIVERED, STATUS_CANCELLED, STATUS_FAILED)
RETRYABLE_STATES = (STATUS_FAILED, STATUS_CANCELLED, STATUS_TIMEOUT, STATUS_NO_OPINION)


def task_id() -> str:
    return "t_" + uuid.uuid4().hex[:8]


def msg_id() -> str:
    return "m_" + uuid.uuid4().hex[:10]


def execution_key(session_id: str, tid: str, stage: str, member_id: str) -> str:
    return f"{session_id}:{tid}:{stage}:{member_id}"


def ensure_task_fields(session: Dict[str, Any]) -> None:
    """老会话没有任务字段时补齐，避免读旧会议报错。"""
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


def build_prompt(
    stage: str,
    goal: str,
    task_type: str = TYPE_SINGLE,
    artifacts: Optional[List[Dict[str, Any]]] = None,
    opinions: Optional[List[Dict[str, Any]]] = None,
    workspace_dir: str = "",
    quoted: str = "",
) -> str:
    """拼装某环节的完整输入；规程文件随软件提供，服务端读取后注入。"""
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
        self._busy: Dict[Tuple[str, str], bool] = {}
        self._active_keys: Dict[str, Dict[str, Any]] = {}

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
            thread = self._workers.get(key)
            if thread and thread.is_alive():
                return
            thread = threading.Thread(
                target=self._worker_loop, args=(sid, member_id),
                name=f"task-worker-{sid}-{member_id}", daemon=True,
            )
            self._workers[key] = thread
            thread.start()

    def _worker_loop(self, sid: str, member_id: str) -> None:
        key = (sid, member_id)
        try:
            while True:
                with self.store.session_lock(sid):
                    session = self._get(sid)
                    if not session:
                        break
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
                        break
                    task = session["tasks"][tid]
                    task["status"] = STATUS_RUNNING
                    task["started_at"] = int(time.time())
                    self._add_flow(task, f"{member_id} 开始执行")
                    if task.get("result_msg_id"):
                        self._update_message(session, task["result_msg_id"], {"status": STATUS_RUNNING})
                    self._update_card(session, task)
                    self._save(session)

                self._mark_busy(sid, member_id, True)
                try:
                    self._run_task(sid, tid)
                finally:
                    self._mark_busy(sid, member_id, False)

                with self.store.session_lock(sid):
                    session = self._get(sid)
                    if not session:
                        break
                    ensure_task_fields(session)
                    queue = session["task_queues"].get(member_id) or []
                    if tid in queue:
                        queue.remove(tid)
                    self._save(session)
                    has_more = any(
                        (session["tasks"].get(t) or {}).get("status") == STATUS_QUEUED for t in queue
                    )
                if not has_more:
                    break
        finally:
            with self._lock:
                if self._workers.get(key) is threading.current_thread():
                    self._workers.pop(key, None)

    def _mark_busy(self, sid: str, member_id: str, busy: bool) -> None:
        with self._cond:
            self._busy[(sid, member_id)] = busy
            if not busy:
                self._cond.notify_all()

    def _wait_member_idle(self, sid: str, member_id: str) -> None:
        """审核不占用成员队列位置，但要等该成员当前任务跑完（串行）。"""
        with self._cond:
            while self._busy.get((sid, member_id)):
                self._cond.wait(timeout=0.2)

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
            else:
                status = STATUS_DELIVERED
            task["status"] = status
            task["finished_at"] = int(time.time())
            task["artifacts"] = scan_artifacts(workspace_dir, result.get("artifacts") or [])
            task["part_a"]["brief"] = result.get("summary", "")
            task["part_a"]["artifacts"] = task["artifacts"]
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
        self._wait_member_idle(sid, member_id)
        self._mark_busy(sid, member_id, True)
        try:
            result = self._execute(
                sid, tid, STAGE_REVIEW, member_id, settings, workspace_dir,
                read_only=True,
            )
        finally:
            self._mark_busy(sid, member_id, False)

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
            opinions = [
                item for item in (task.get("part_b") or [])
                if item.get("status") == STATUS_DONE and item.get("summary")
            ] if stage == STAGE_DELIVER else None
            prompt = build_prompt(
                stage=stage,
                goal=task.get("goal", ""),
                task_type=task.get("type", TYPE_SINGLE),
                artifacts=task.get("artifacts") or [],
                opinions=opinions,
                workspace_dir=workspace_dir,
                quoted=task.get("quoted") or "",
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
            task.setdefault("attempts", {})[
                f"{stage}:{member_id or task.get('owner_id')}"
            ] = task["attempts"].get(f"{stage}:{member_id or task.get('owner_id')}", 0) + 1
            self._update_card(session, task)
            self._save(session)

        key = execution_key(sid, tid, stage, emp_id or "")
        with self._lock:
            self._active_keys[key] = {"sid": sid, "tid": tid, "stage": stage, "member_id": member_id}

        runner_emp = dict(emp)
        if read_only:
            runner_emp["write_access"] = False  # 审核环节一律只读

        started = time.time()
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

        status = str(getattr(res, "status", "") or "")
        raw = str(getattr(res, "raw_output", "") or "") or str(getattr(res, "output", "") or "")
        parsed = parse_output(str(getattr(res, "output", "") or ""))
        elapsed = round(time.time() - started, 2)

        if status in ("interrupted",) or key in CANCELLED_KEYS:
            mapped = "cancelled"
        elif status in ("timeout",):
            mapped = STATUS_TIMEOUT
        elif status in ("error", "failed"):
            mapped = STATUS_FAILED
        else:
            # 执行记录代表「这一次执行完成」，不是整个任务已交付；任务终态由各阶段写回
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
    def cancel(self, sid: str, tid: str, stage: str, member_id: str = "") -> Dict[str, Any]:
        """静默取消：终止进程、状态转已取消、不追加系统消息、不弹窗。"""
        with self.store.session_lock(sid):
            session = self._get(sid)
            if not session:
                return {"error": "会话不存在"}
            task = (session.get("tasks") or {}).get(tid)
            if not task:
                return {"error": "任务不存在"}
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
            self._add_flow(task, "已取消")
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
                current = task["part_a"].get("final_status")
                # 交付：完成后也可重试，用于重新生成交付内容
                allowed = current in (STATUS_DONE, *RETRYABLE_STATES) or \
                    task.get("status") in (STATUS_DELIVERED, *RETRYABLE_STATES)
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

        thread = threading.Thread(
            target=self._retry_worker, args=(sid, tid, stage, member_id),
            name=f"task-retry-{tid}-{stage}", daemon=True,
        )
        thread.start()
        return {"ok": True}

    def _retry_worker(self, sid: str, tid: str, stage: str, member_id: str) -> None:
        settings = self.store.load_settings()
        with self.store.session_lock(sid):
            session = self._get(sid)
            workspace_dir = (session or {}).get("workspace_dir", "")
        if stage == STAGE_EXEC:
            ok = self._run_exec(sid, tid, settings, workspace_dir)
            if ok:
                with self.store.session_lock(sid):
                    session = self._get(sid)
                    if session and tid in (session.get("tasks") or {}):
                        self._save(session)
                self._run_reviews(sid, tid, settings, workspace_dir)
                self._run_deliver(sid, tid, settings, workspace_dir)
        elif stage == STAGE_REVIEW:
            self._wait_member_idle(sid, member_id)
            self._mark_busy(sid, member_id, True)
            try:
                self._run_review_one(sid, tid, member_id, settings, workspace_dir)
            finally:
                self._mark_busy(sid, member_id, False)
            # 重试审核只更新该成员的意见，不重做交付，也不改任务终态
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
                    if task.get("status") == STATUS_RUNNING:
                        task["status"] = STATUS_CANCELLED
                        task["finished_at"] = int(time.time())
                        self._add_flow(task, "服务重启，进行中的任务已取消，可重试")
                        self._update_card(session, task)
                        changed = True
                        recovered += 1
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
