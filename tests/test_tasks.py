"""AI 团队任务系统自动化测试。

运行：python3 -m unittest discover -s tests -v
全部使用内存 FakeStore + FakeRunner，不调用真实 CLI、不写真实 data/。
"""

import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.storage as storage
from core import tasks as T

# 网络接口测试也复用这套隔离目录，避免读写用户真实会话
_TMP_DIR = tempfile.mkdtemp(prefix="aim-task-test-")
storage.SESSIONS_DIR = _TMP_DIR
storage.SETTINGS_FILE = os.path.join(_TMP_DIR, "settings.json")


def make_settings():
    def emp(eid, name):
        return {
            "id": eid, "name": name, "avatar": "🤖", "type": "cli",
            "cli_cmd": 'echo "{prompt}"', "system_prompt": "",
            "enabled": True, "timeout": 0, "write_access": True,
        }
    return {
        "ui_language": "zh",
        "employees": [
            emp("emp_agy", "agy"),
            emp("emp_ds4", "DS4"),
            emp("emp_hy4", "HY4"),
        ],
    }


def make_session(sid="s1", workspace_dir=""):
    return {
        "id": sid,
        "title": "测试会话",
        "status": "idle",
        "messages": [],
        "workspace_dir": workspace_dir or _TMP_DIR,
        "employee_ids": ["emp_agy", "emp_ds4", "emp_hy4"],
        "tasks": {},
        "task_queues": {},
        "active_task_id": None,
    }


class FakeStore:
    """模拟磁盘：每次读写都给一份深拷贝，贴近真实的读-改-写语义。"""

    def __init__(self, session=None, settings=None):
        self.settings = settings or make_settings()
        self.sessions = {}
        if session:
            self.sessions[session["id"]] = copy.deepcopy(session)
        self.lock = threading.RLock()

    def get_session(self, sid):
        data = self.sessions.get(sid)
        return copy.deepcopy(data) if data is not None else None

    def save_session(self, session):
        self.sessions[session["id"]] = copy.deepcopy(session)

    def session_lock(self, sid):
        return self.lock

    def load_settings(self):
        return copy.deepcopy(self.settings)

    def list_sessions(self):
        return [{"id": sid} for sid in self.sessions]


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.review_barrier = None
        self.exec_barrier = None
        self.gates = {}
        self.killed = set()
        self.fail_for = []
        self.empty_for = []
        self.timeout_for = []
        # 按 key 片段返回自定义输出，用于验证「正文 + 登记块」这类新协议
        self.outputs = {}
        # 按 key 片段返回「每次调用依次不同」的输出，用于验证语义层重试（先开场白、后正常）
        self.sequence_outputs = {}
        # 每位成员同时在跑的调用数（不变量：任意成员永远不超过 1）
        self.per_member = {}
        self.max_per_member = {}
        # 进审核就挡住：用来在「审核进行中」这一刻检查卡片状态（进门后才放行）
        self.review_gate = None

    def __call__(self, employee, prompt, session_id, workspace_dir):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            member = employee.get("id")
            live = self.per_member.get(member, 0) + 1
            self.per_member[member] = live
            self.max_per_member[member] = max(self.max_per_member.get(member, 0), live)
            self.calls.append({
                "employee": dict(employee),
                "prompt": prompt,
                "key": session_id,
                "workspace_dir": workspace_dir,
                "at": time.time(),
            })
        try:
            if ":review:" in session_id and self.review_barrier is not None:
                try:
                    self.review_barrier.wait(timeout=3)
                except threading.BrokenBarrierError:
                    pass
            if ":exec:" in session_id and self.exec_barrier is not None:
                try:
                    self.exec_barrier.wait(timeout=3)
                except threading.BrokenBarrierError:
                    pass
            if ":review:" in session_id and self.review_gate is not None:
                self.review_gate.wait(timeout=5)
            gate = self.gates.get(session_id)
            if gate is not None:
                gate.wait(timeout=3)
            if session_id in self.killed:
                return SimpleNamespace(status="interrupted", output="", raw_output="", elapsed=0)
            if any(flag in session_id for flag in self.timeout_for):
                return SimpleNamespace(status="timeout", output="", raw_output="超时无输出", elapsed=600)
            if any(flag in session_id for flag in self.fail_for):
                return SimpleNamespace(status="error", output="调用失败", raw_output="调用失败", elapsed=0)
            if any(flag in session_id for flag in self.empty_for):
                return SimpleNamespace(status="success", output="", raw_output="", elapsed=0)
            for flag, seq in self.sequence_outputs.items():
                if flag in session_id and seq:
                    text = seq.pop(0)
                    return SimpleNamespace(status="success", output=text, raw_output=text, elapsed=0)
            for flag, text in self.outputs.items():
                if flag in session_id:
                    return SimpleNamespace(status="success", output=text, raw_output=text, elapsed=0)
            payload = {
                "summary": "已完成 " + session_id,
                "artifacts": [{"path": "out.md", "summary": "产物", "bytes": 12}],
                "changes": "按意见改了一处",
            }
            text = json.dumps(payload, ensure_ascii=False)
            return SimpleNamespace(status="success", output=text, raw_output=text, elapsed=0)
        finally:
            with self.lock:
                self.active -= 1
                member = employee.get("id")
                self.per_member[member] = self.per_member.get(member, 1) - 1


class TaskCase(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore(make_session())
        self.runner = FakeRunner()
        self.killed_keys = []

        def killer(key):
            self.killed_keys.append(key)
            self.runner.killed.add(key)
            gate = self.runner.gates.get(key)
            if gate is not None:
                gate.set()

        self.manager = T.TaskManager(store=self.store, runner=self.runner, killer=killer)
        self.settings = self.store.load_settings()
        T.CANCELLED_KEYS.clear()

    def _submit(self, content, sid="s1"):
        return self.manager.submit_message(sid, content, self.settings)

    def _task(self, tid, sid="s1"):
        return self.store.get_session(sid)["tasks"].get(tid)

    def _wait(self, tid, timeout=6):
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = self._task(tid)
            if task and task.get("status") in (T.STATUS_DELIVERED, T.STATUS_CANCELLED,
                                               T.STATUS_FAILED, T.STATUS_TIMEOUT):
                return task
            time.sleep(0.02)
        return self._task(tid)

    def _wait_calls(self, count, timeout=6):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.runner.lock:
                if len(self.runner.calls) >= count:
                    return
            time.sleep(0.02)

    def _wait_retries(self, timeout=8):
        """等所有重试线程（含它内部同步跑的自动补交付）真的结束。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.manager._lock:
                if not self.manager._retries and not self.manager._active_keys:
                    return
            time.sleep(0.02)
        self.manager.wait_idle()


class TestCollabParsing(TaskCase):
    def test_missing_owner_is_rejected(self):
        parsed = T.parse_collab("/collab 帮我写个方案", self.settings)
        self.assertFalse(parsed["ok"])
        self.assertIn("主创", parsed["error"])

    def test_missing_reviewer_is_rejected(self):
        parsed = T.parse_collab("/collab @agy 帮我写个方案", self.settings)
        self.assertFalse(parsed["ok"])
        self.assertIn("协助", parsed["error"])

    def test_missing_goal_is_rejected(self):
        parsed = T.parse_collab("/collab @agy @DS4", self.settings)
        self.assertFalse(parsed["ok"])
        self.assertIn("任务内容", parsed["error"])

    def test_valid_collab_strips_prefix_and_dedupes(self):
        parsed = T.parse_collab("/collab @agy @DS4 @HY4 写一份方案", self.settings)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["owner_id"], "emp_agy")
        self.assertEqual(parsed["reviewer_ids"], ["emp_ds4", "emp_hy4"])
        self.assertEqual(parsed["goal"], "写一份方案")

    def test_duplicate_mentions_do_not_create_duplicate_reviewers(self):
        parsed = T.parse_collab("/collab @agy @DS4 @DS4 @HY4 做事", self.settings)
        self.assertEqual(parsed["reviewer_ids"], ["emp_ds4", "emp_hy4"])

    def test_submit_rejected_collab_keeps_user_message(self):
        result = self._submit("/collab @agy 一个人干活")
        self.assertTrue(result.get("validation_error"))
        session = self.store.get_session("s1")
        self.assertEqual(len(session["tasks"]), 0)
        self.assertEqual(len(session["messages"]), 1)
        self.assertEqual(session["messages"][0]["sender"], "user")

    def test_disabled_reviewer_is_rejected_before_submit(self):
        # 协助成员停用时就该拦在输入阶段：否则任务照样发起，审核环节才落一个「成员不可用」
        for emp in self.settings["employees"]:
            if emp["id"] == "emp_hy4":
                emp["enabled"] = False
        parsed = T.parse_collab("/collab @agy @DS4 @HY4 写方案", self.settings)
        self.assertFalse(parsed["ok"])
        self.assertIn("HY4", parsed["error"])
        self.assertIn("停用", parsed["error"])
        # 被点名的协助成员中只要有一位不可用就不创建任务
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        self.assertTrue(result.get("validation_error"))
        self.assertIn("HY4", result["error"])
        self.assertEqual(len(self.store.get_session("s1")["tasks"]), 0)

    def test_reviewer_without_cli_is_rejected(self):
        for emp in self.settings["employees"]:
            if emp["id"] == "emp_ds4":
                emp["cli_cmd"] = ""
        parsed = T.parse_collab("/collab @agy @DS4 写方案", self.settings)
        self.assertFalse(parsed["ok"])
        self.assertIn("DS4", parsed["error"])

    def test_disabled_owner_is_rejected(self):
        for emp in self.settings["employees"]:
            if emp["id"] == "emp_agy":
                emp["enabled"] = False
        parsed = T.parse_collab("/collab @agy @DS4 写方案", self.settings)
        self.assertFalse(parsed["ok"])
        self.assertIn("主创", parsed["error"])

    def test_mentions_may_be_spread_across_the_sentence(self):
        # 用户常见的写法：先点主创并说一句，再把审核的人放在后面
        parsed = T.parse_collab("/collab @agy 你是主创，@DS4 @HY4 负责审核", self.settings)
        self.assertTrue(parsed["ok"], parsed["error"])
        self.assertEqual(parsed["owner_id"], "emp_agy")
        self.assertEqual(parsed["reviewer_ids"], ["emp_ds4", "emp_hy4"])
        self.assertEqual(parsed["goal"], "你是主创，负责审核")

    def test_file_reference_style_mentions_are_accepted(self):
        # 微信/编辑器里 @//agy/ 这种带路径装饰的写法，以及小写的成员目录名
        parsed = T.parse_collab(
            "/collab @//agy/ 针对 E01 写出阶段二，@//ds4/ @//hy4/ 负责审核",
            self.settings,
        )
        self.assertTrue(parsed["ok"], parsed["error"])
        self.assertEqual(parsed["owner_id"], "emp_agy")
        self.assertEqual(parsed["reviewer_ids"], ["emp_ds4", "emp_hy4"])
        self.assertEqual(parsed["goal"], "针对 E01 写出阶段二，负责审核")

    def test_dashed_full_name_with_parenthesis_is_accepted(self):
        # 真实配置里成员名是「DeepSeek-V4 (DS4)」，用户按目录名写成 @//deepseek-v4/(DS4)
        for emp in self.settings["employees"]:
            if emp["id"] == "emp_ds4":
                emp["name"] = "DeepSeek-V4 (DS4)"
            if emp["id"] == "emp_hy4":
                emp["name"] = "混元-4 (HY4)"
        parsed = T.parse_collab(
            "/collab @//agy/ 写阶段二，@//deepseek-v4/(DS4) @混元-4 (HY4) 负责审核",
            self.settings,
        )
        self.assertTrue(parsed["ok"], parsed["error"])
        self.assertEqual(parsed["owner_id"], "emp_agy")
        self.assertEqual(parsed["reviewer_ids"], ["emp_ds4", "emp_hy4"])

    def test_picked_from_member_list_then_spread_in_sentence(self):
        # 用户真实输入：成员是从候选列表点出来的全名（带空格和括号），
        # 主创在开头说一句，审核的人写在句子后半段
        for emp in self.settings["employees"]:
            if emp["id"] == "emp_ds4":
                emp["name"] = "DeepSeek-V4 (DS4)"
            if emp["id"] == "emp_hy4":
                emp["name"] = "混元-4 (HY4)"
        parsed = T.parse_collab(
            "/collab @agy 你是主创，针对 E01 的问题写出阶段二的内容，"
            "@DeepSeek-V4 (DS4) @混元-4 (HY4) 负责审核",
            self.settings,
        )
        self.assertTrue(parsed["ok"], parsed["error"])
        self.assertEqual(parsed["owner_id"], "emp_agy")
        self.assertEqual(parsed["reviewer_ids"], ["emp_ds4", "emp_hy4"])
        self.assertIn("针对 E01 的问题写出阶段二的内容", parsed["goal"])

    def test_mentions_are_case_insensitive(self):
        parsed = T.parse_collab("/collab @AGY @ds4 写方案", self.settings)
        self.assertTrue(parsed["ok"], parsed["error"])
        self.assertEqual(parsed["owner_id"], "emp_agy")
        self.assertEqual(parsed["reviewer_ids"], ["emp_ds4"])

    def test_first_mention_wins_for_single_task(self):
        # 一条消息里点了三个人时，执行的是第一个 @ 的人，而不是名字最长的那个
        self.assertEqual(
            T.parse_single("@agy 你做攻略，@DS4 选酒店，@HY4 查票", self.settings),
            "emp_agy",
        )

    def test_email_and_path_are_not_mentions(self):
        self.assertIsNone(T.parse_single("把结果发到 a@b.com 就行", self.settings))
        self.assertIsNone(T.parse_single("看下 src/app.js 的写法", self.settings))


class TestTaskCreation(TaskCase):
    def test_single_task_structure(self):
        self.manager.auto_start = False  # 先检查刚入队的任务结构
        result = self._submit("@agy 做一件事")
        self.assertIn("task_id", result)
        session = self.store.get_session("s1")
        task = session["tasks"][result["task_id"]]
        self.assertEqual(task["type"], T.TYPE_SINGLE)
        self.assertEqual(task["status"], T.STATUS_QUEUED)
        self.assertEqual(session["active_task_id"], result["task_id"])
        self.assertEqual(session["task_queues"]["emp_agy"], [result["task_id"]])
        result_msg = [m for m in session["messages"] if m.get("kind") == "task_result"][0]
        self.assertEqual(result_msg["task_id"], result["task_id"])
        self.assertTrue(result_msg["in_chat"])
        self.assertIn("sent_prompt", result_msg)
        self.assertIn("raw_response", result_msg)
        self.manager.wait_idle()

    def test_single_result_msg_shows_prompt_while_running(self):
        # 单任务（如给 agy 发消息）：结果消息在执行进行中就要带上 sent_prompt 与 started_at，
        # 这样点 ℹ️「交互详情审查」能立即看到「发送给 AI 的 Prompt」，结果消息顶部也能实时计时，
        # 不用等整轮跑完。
        result = self._submit("@agy 做一件事")
        tid = result["task_id"]
        # 预先按 execution_key 把 worker 卡在「执行中」：runner 一进来就会被 gate 挡住，
        # 但此刻 _execute 已经把 Prompt 与 started_at 写进了结果消息
        hold = threading.Event()
        self.runner.gates[f"s1:{tid}:exec:emp_agy"] = hold
        # 等 worker 进入执行调用并被门挡住
        deadline = time.time() + 4
        while time.time() < deadline and not self.runner.calls:
            time.sleep(0.02)
        session = self.store.get_session("s1")
        result_msg = [m for m in session["messages"] if m.get("kind") == "task_result"][0]
        self.assertEqual(result_msg["status"], T.STATUS_RUNNING)
        self.assertTrue(result_msg["sent_prompt"], "执行中结果消息应已带 Prompt")
        self.assertTrue(result_msg["started_at"], "执行中结果消息应已带 started_at 供实时计时")
        # 放行并等完成：完成后补上 finished_at
        hold.set()
        self._wait(tid)
        session = self.store.get_session("s1")
        result_msg = [m for m in session["messages"] if m.get("kind") == "task_result"][0]
        self.assertEqual(result_msg["status"], T.STATUS_DELIVERED)
        self.assertTrue(result_msg["finished_at"], "完成后结果消息应带 finished_at")
        self.manager.wait_idle()

    def test_collab_with_spread_mentions_creates_card_with_reviewers(self):
        # 用户原话：主创点在前，审核的人写在句子后半段
        self.manager.auto_start = False
        result = self._submit("/collab @agy 你是主创，写阶段二，@DS4 @HY4 负责审核")
        self.assertIn("task_id", result, result)
        session = self.store.get_session("s1")
        task = session["tasks"][result["task_id"]]
        self.assertEqual(task["type"], T.TYPE_COLLAB)
        self.assertEqual(task["owner_id"], "emp_agy")
        self.assertEqual([i["member_id"] for i in task["part_b"]], ["emp_ds4", "emp_hy4"])
        self.assertIn("写阶段二", task["goal"])
        cards = [m for m in session["messages"] if m.get("kind") == "task_card"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["task_id"], result["task_id"])

    def test_no_mention_is_context_only(self):
        # 本场会话还没有任何 AI 回过话，没有「默认接话对象」——仍只作上下文保留。
        # 有历史时默认接最近一位，见 TestDefaultRouteToLastAi。
        result = self._submit("大家先看看这段背景")
        self.assertEqual(result.get("info"), "context_only")
        session = self.store.get_session("s1")
        self.assertEqual(len(session["tasks"]), 0)
        self.assertEqual(session["messages"][-1]["content"], "大家先看看这段背景")

    def test_reply_targets_quoted_member(self):
        self.store.sessions["s1"]["messages"].append({
            "id": "m_old", "sender": "emp_ds4", "sender_name": "DS4",
            "content": "上一版结论", "in_chat": True,
        })
        result = self.manager.submit_message(
            "s1", "再补一版", self.settings, reply_to="m_old", forced_owner_id="emp_ds4"
        )
        task = self._task(result["task_id"])
        self.assertEqual(task["owner_id"], "emp_ds4")
        self.assertEqual(task["quoted"], "上一版结论")
        self.manager.wait_idle()

    def test_collab_task_creates_card(self):
        self.manager.auto_start = False  # 卡片在创建时就应出现且为排队中
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        self.assertEqual(result["type"], T.TYPE_COLLAB)
        session = self.store.get_session("s1")
        card = [m for m in session["messages"] if m.get("kind") == "task_card"][0]
        self.assertTrue(card["in_chat"])
        self.assertEqual(card["card"]["status"], T.STATUS_QUEUED)
        self.assertEqual(len(card["card"]["part_b"]), 2)
        self.assertTrue(card["card"]["flow"])
        self.manager.wait_idle()


class TestQueues(TaskCase):
    def test_same_member_fifo_serial(self):
        gate = threading.Event()
        first_key = None
        self.runner.gates = {}

        # 第一条任务的执行阻塞住，观察第二条是否被提前执行
        original_runner = self.runner

        def gated_runner(employee, prompt, session_id, workspace_dir):
            if not original_runner.gates:
                original_runner.gates[session_id] = gate
            return original_runner(employee, prompt, session_id, workspace_dir)

        self.manager.runner = gated_runner
        r1 = self._submit("@agy 第一件")
        r2 = self._submit("@agy 第二件")
        self._wait_calls(1)
        with self.runner.lock:
            self.assertEqual(len(self.runner.calls), 1)
            first_key = self.runner.calls[0]["key"]
        gate.set()
        t1 = self._wait(r1["task_id"])
        t2 = self._wait(r2["task_id"])
        self.assertEqual(t1["status"], T.STATUS_DELIVERED)
        self.assertEqual(t2["status"], T.STATUS_DELIVERED)
        self.assertEqual(self.runner.max_active, 1)
        with self.runner.lock:
            keys = [c["key"] for c in self.runner.calls]
        self.assertIn(r1["task_id"], keys[0])
        self.assertIn(r2["task_id"], keys[1])
        self.manager.wait_idle()

    def test_different_members_run_in_parallel(self):
        self.runner.exec_barrier = threading.Barrier(2)  # 两个成员都进入执行才算并行
        r1 = self._submit("@agy A 的事")
        r2 = self._submit("@DS4 B 的事")
        self._wait(r1["task_id"])
        self._wait(r2["task_id"])
        self.assertGreaterEqual(self.runner.max_active, 2)
        self.manager.wait_idle()

    def test_review_members_run_in_parallel(self):
        self.runner.review_barrier = threading.Barrier(2)
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        task = self._wait(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_DELIVERED)
        with self.runner.lock:
            review_calls = [c for c in self.runner.calls if ":review:" in c["key"]]
        self.assertEqual(len(review_calls), 2)
        self.assertGreaterEqual(self.runner.max_active, 2)
        self.manager.wait_idle()

    def test_queue_exposes_pending_order(self):
        # 界面靠 task_queues + tasks.status 展示「该成员队列第 n / m 项」
        gate = threading.Event()
        original_runner = self.runner

        def gated_runner(employee, prompt, session_id, workspace_dir):
            if not original_runner.gates:
                original_runner.gates[session_id] = gate
            return original_runner(employee, prompt, session_id, workspace_dir)

        self.manager.runner = gated_runner
        r1 = self._submit("@agy 第一件")
        r2 = self._submit("@agy 第二件")
        r3 = self._submit("@agy 第三件")
        self._wait_calls(1)
        session = self.store.get_session("s1")
        queue = session["task_queues"]["emp_agy"]
        self.assertEqual(queue, [r1["task_id"], r2["task_id"], r3["task_id"]])
        self.assertEqual(
            [session["tasks"][t]["status"] for t in queue],
            [T.STATUS_RUNNING, T.STATUS_QUEUED, T.STATUS_QUEUED],
        )
        gate.set()
        for r in (r1, r2, r3):
            self._wait(r["task_id"])
        self.manager.wait_idle()

    def test_queued_task_waits_for_member_current_task(self):
        self.runner.review_barrier = threading.Barrier(2)
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        task = self._wait(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_DELIVERED)
        self.manager.wait_idle()


class TestCollabFlow(TaskCase):
    def test_three_stages_and_card(self):
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        task = self._wait(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_DELIVERED)
        self.assertEqual(task["step"], 3)
        self.assertTrue(task["part_a"]["brief"])
        self.assertTrue(task["part_a"]["final"]["summary"])
        self.assertEqual(len(task["part_b"]), 2)
        for item in task["part_b"]:
            self.assertEqual(item["status"], T.STATUS_DONE)
            self.assertTrue(item["summary"])
            self.assertTrue(item["sent_prompt"])
            self.assertTrue(item["raw_output"])
        session = self.store.get_session("s1")
        card = [m for m in session["messages"] if m.get("kind") == "task_card"][0]["card"]
        self.assertEqual(card["status"], T.STATUS_DELIVERED)
        self.assertIn("part_a", card)
        self.assertTrue(card["flow"])
        self.assertTrue(card["started_at"])
        records = [m for m in session["messages"] if m.get("in_chat") is False]
        self.assertEqual(len(records), 4)  # 执行 + 2 审核 + 交付
        for rec in records:
            self.assertTrue(rec["sent_prompt"])
            self.assertTrue(rec["raw_response"])
        self.manager.wait_idle()

    def test_review_is_read_only(self):
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        self._wait(result["task_id"])
        with self.runner.lock:
            review_calls = [c for c in self.runner.calls if ":review:" in c["key"]]
            exec_calls = [c for c in self.runner.calls if ":exec:" in c["key"]]
        self.assertTrue(all(c["employee"]["write_access"] is False for c in review_calls))
        self.assertTrue(all(c["employee"]["write_access"] is True for c in exec_calls))
        self.manager.wait_idle()

    def test_failed_reviewer_still_delivers(self):
        self.runner.fail_for = [":review:emp_ds4"]
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        task = self._wait(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_DELIVERED)
        statuses = {i["member_id"]: i["status"] for i in task["part_b"]}
        self.assertEqual(statuses["emp_ds4"], T.STATUS_FAILED)
        self.assertEqual(statuses["emp_hy4"], T.STATUS_DONE)
        self.manager.wait_idle()

    def test_empty_review_marked_no_opinion(self):
        self.runner.empty_for = [":review:emp_ds4"]
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        task = self._wait(result["task_id"])
        statuses = {i["member_id"]: i["status"] for i in task["part_b"]}
        self.assertEqual(statuses["emp_ds4"], T.STATUS_NO_OPINION)
        self.manager.wait_idle()

    def test_review_prompt_contains_real_artifacts_and_opinions(self):
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        self._wait(result["task_id"])
        with self.runner.lock:
            calls = list(self.runner.calls)
        review_prompt = next(c["prompt"] for c in calls if ":review:" in c["key"])
        deliver_prompt = next(c["prompt"] for c in calls if ":deliver:" in c["key"])
        self.assertIn("只读", review_prompt)
        self.assertIn("产物清单", review_prompt)
        self.assertIn("审核意见", deliver_prompt)
        self.assertIn("写方案", deliver_prompt)
        self.manager.wait_idle()


class TestCancelRetry(TaskCase):
    def test_cancel_is_silent_and_keeps_files(self):
        gate = threading.Event()
        self.runner.gates = {}
        original = self.runner

        def gated(employee, prompt, session_id, workspace_dir):
            original.gates.setdefault(session_id, gate)
            return original(employee, prompt, session_id, workspace_dir)

        self.manager.runner = gated
        result = self._submit("@agy 写文件")
        self._wait_calls(1)
        before = len(self.store.get_session("s1")["messages"])
        out = self.manager.cancel("s1", result["task_id"], T.STAGE_EXEC)
        self.assertTrue(out["ok"])
        gate.set()
        task = self._wait(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_CANCELLED)
        self.assertTrue(self.killed_keys)  # 向执行进程发终止信号
        session = self.store.get_session("s1")
        self.assertEqual(len([m for m in session["messages"] if m.get("sender") == "system"
                              and m.get("kind") != "task_card"]), 0)
        self.assertEqual(len(session["messages"]), before)  # 取消不追加系统播报
        # 取消前已写入的文件自然保留
        path = os.path.join(_TMP_DIR, "keep.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("keep")
        self.assertTrue(os.path.exists(path))
        self.manager.wait_idle()

    def test_cancel_one_reviewer_does_not_stop_delivery(self):
        # 让 DS4 的审核真的停在「进行中」，再取消（直接点太快会撞上「已结束不能取消」）
        gate = threading.Event()
        original = self.runner

        def gated(employee, prompt, session_id, workspace_dir):
            if ":review:emp_ds4" in session_id:
                original.gates.setdefault(session_id, gate)
            return original(employee, prompt, session_id, workspace_dir)

        self.manager.runner = gated
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        deadline = time.time() + 6
        while time.time() < deadline:
            task = self._task(result["task_id"])
            if any(i["member_id"] == "emp_ds4" and i["status"] == T.MEMBER_IN_PROGRESS
                   for i in task["part_b"]):
                break
            time.sleep(0.02)
        out = self.manager.cancel("s1", result["task_id"], T.STAGE_REVIEW, "emp_ds4")
        self.assertTrue(out["ok"])
        gate.set()
        task = self._wait(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_DELIVERED)
        statuses = {i["member_id"]: i["status"] for i in task["part_b"]}
        self.assertEqual(statuses["emp_ds4"], T.STATUS_CANCELLED)
        self.manager.wait_idle()

    def test_retry_single_reruns_same_task(self):
        self.runner.fail_for = [":exec:"]
        result = self._submit("@agy 做一件事")
        task = self._wait(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_FAILED)
        self.runner.fail_for = []
        T.CANCELLED_KEYS.clear()
        retry = self.manager.retry("s1", result["task_id"], T.STAGE_EXEC)
        self.assertTrue(retry["ok"])
        task = self._wait(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_DELIVERED)
        session = self.store.get_session("s1")
        self.assertEqual(len(session["tasks"]), 1)  # 不新增任务
        self.manager.wait_idle()

    def test_retry_exec_reruns_reviews_and_delivery(self):
        # 主创执行失败：只有一次执行，没有审核与交付
        self.runner.fail_for = [":exec:"]
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        task = self._wait(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_FAILED)
        with self.runner.lock:
            self.assertEqual(len([c for c in self.runner.calls if ":exec:" in c["key"]]), 1)
            self.assertEqual(len([c for c in self.runner.calls if ":review:" in c["key"]]), 0)

        # 重试执行：这一轮从审核继续推进，不新增任务
        self.runner.fail_for = []
        retry = self.manager.retry("s1", result["task_id"], T.STAGE_EXEC)
        self.assertTrue(retry["ok"])
        task = self._wait(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_DELIVERED)
        self.assertEqual(task["step"], 3)
        self.assertTrue(all(i["status"] == T.STATUS_DONE for i in task["part_b"]))
        self.assertTrue(task["part_a"]["final"]["summary"])
        with self.runner.lock:
            self.assertEqual(len([c for c in self.runner.calls if ":exec:" in c["key"]]), 2)
            self.assertEqual(len([c for c in self.runner.calls if ":review:" in c["key"]]), 2)
            self.assertEqual(len([c for c in self.runner.calls if ":deliver:" in c["key"]]), 1)
        self.assertEqual(len(self.store.get_session("s1")["tasks"]), 1)
        self.manager.wait_idle()

    def test_retry_review_only_updates_that_member(self):
        # 审核失败 → 重试该成员：只重跑这一位；两位审核都出结论后自动补一次交付
        self.runner.fail_for = [":review:emp_ds4"]
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        self._wait(result["task_id"])
        with self.runner.lock:
            before_deliver = len([c for c in self.runner.calls if ":deliver:" in c["key"]])
        self.runner.fail_for = []
        retry = self.manager.retry("s1", result["task_id"], T.STAGE_REVIEW, "emp_ds4")
        self.assertTrue(retry["ok"])
        # 重试必须真的跑起来（旧实现里重试线程会等自己空闲，永久卡住）
        deadline = time.time() + 8
        reviews = 0
        while time.time() < deadline:
            with self.runner.lock:
                reviews = len([c for c in self.runner.calls
                               if ":review:emp_ds4" in c["key"]])
            if reviews >= 2:
                break
            time.sleep(0.02)
        self.manager.wait_idle()
        self.assertEqual(reviews, 2)
        # 自动补交付是重试线程收尾时才起的另一段，wait_idle 之后可能还没进到执行器：
        # 这里等它真的跑起来再数，否则偶发少一次（1 != 2）
        deadline = time.time() + 8
        delivers = before_deliver
        while time.time() < deadline:
            with self.runner.lock:
                delivers = len([c for c in self.runner.calls if ":deliver:" in c["key"]])
            if delivers >= before_deliver + 1:
                break
            time.sleep(0.02)
        self.assertEqual(delivers, before_deliver + 1)
        task = self._wait(result["task_id"], timeout=5) or self._task(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_DELIVERED)
        self.assertEqual(
            next(i["status"] for i in task["part_b"] if i["member_id"] == "emp_ds4"),
            T.STATUS_DONE,
        )
        # 这一次交付是系统补的：卡片要能标出「审核补全后重新生成」
        self.assertEqual(task.get("deliver_source"), "review_auto")
        self.assertFalse(task.get("deliver_stale"))

    def test_redo_exec_on_delivered_collab_restarts_round(self):
        # 已交付后点执行段的「重新执行」：整轮重来（执行 → 审核 → 交付），不新增任务
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        self._wait(result["task_id"])

        def count(part):
            with self.runner.lock:
                return len([c for c in self.runner.calls if f":{part}:" in c["key"]])

        before = {p: count(p) for p in ("exec", "review", "deliver")}
        retry = self.manager.retry("s1", result["task_id"], T.STAGE_EXEC)
        self.assertTrue(retry["ok"])
        task = self._wait(result["task_id"])
        self.manager.wait_idle()
        after = {p: count(p) for p in ("exec", "review", "deliver")}
        self.assertEqual(after["exec"], before["exec"] + 1)
        self.assertEqual(after["review"], before["review"] + 2)
        self.assertEqual(after["deliver"], before["deliver"] + 1)
        self.assertEqual(task["status"], T.STATUS_DELIVERED)
        self.assertTrue(all(i["status"] == T.STATUS_DONE for i in task["part_b"]))
        # 已经成功过的环节重跑，流水里写「重新生成」而不是「重试」
        flow = [f["text"] for f in task["flow"]]
        self.assertIn("重新生成执行", flow)
        self.assertEqual(len(self.store.get_session("s1")["tasks"]), 1)

    def test_redo_review_on_delivered_collab_auto_regenerates_delivery(self):
        # 已交付后点某位审核的「重新审核」：只重跑这一位；全部审核有结论后自动补一次交付，
        # 并把这版终稿标成「审核补全后重新生成」（用户认可这个行为，但要看得出来源）
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        self._wait(result["task_id"])

        def count_review():
            with self.runner.lock:
                return len([c for c in self.runner.calls if ":review:emp_ds4" in c["key"]])

        def count_deliver():
            with self.runner.lock:
                return len([c for c in self.runner.calls if ":deliver:" in c["key"]])

        before_review, before_deliver = count_review(), count_deliver()
        retry = self.manager.retry("s1", result["task_id"], T.STAGE_REVIEW, "emp_ds4")
        self.assertTrue(retry["ok"])
        # 刚点下去：卡片回到「审核中」，旧意见被清掉；终稿内容保留不动
        task = self._task(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_RUNNING)
        self.assertEqual(task["part_a"]["final_status"], T.STATUS_DONE)
        self.assertFalse(task.get("deliver_stale"))
        self.assertEqual(
            next(i["status"] for i in task["part_b"] if i["member_id"] == "emp_ds4"),
            T.MEMBER_WAITING,
        )
        self._wait(result["task_id"])
        self.manager.wait_idle()
        self.assertEqual(count_review(), before_review + 1)
        # 用户没点交付，但所有审核都出了结论 → 系统自动补一次，且只补一次
        self.assertEqual(count_deliver(), before_deliver + 1)
        task = self._task(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_DELIVERED)
        self.assertEqual(task["part_a"]["final_status"], T.STATUS_DONE)
        # 重出成功后过期标记解除；流水里能看出这次是自动补的
        self.assertFalse(task.get("deliver_stale"))
        self.assertEqual(task.get("deliver_source"), "review_auto")
        self.assertEqual(task["part_a"].get("final_source"), "review_auto")
        self.assertTrue(task.get("deliver_source_at"))
        self.assertEqual(task.get("auto_deliver_generation"), task.get("review_generation"))
        flow = [f["text"] for f in task["flow"]]
        self.assertIn("全部审核已出结论，自动补一次交付", flow)
        self.assertTrue(any("系统自动补一次交付" in x for x in flow))
        # 前端拿到的是同一份标记
        card = T.build_card(task)
        self.assertEqual(card["deliver_source"], "review_auto")
        self.assertTrue(card["created_at"])

    def test_two_review_retries_supplement_delivery_only_once(self):
        # 两位审核一起重跑：结论齐了只自动补一次交付，不能重出两份终稿
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        self._wait(result["task_id"])
        with self.runner.lock:
            before = len([c for c in self.runner.calls if ":deliver:" in c["key"]])
        self.assertTrue(self.manager.retry("s1", result["task_id"], T.STAGE_REVIEW, "emp_ds4")["ok"])
        self.assertTrue(self.manager.retry("s1", result["task_id"], T.STAGE_REVIEW, "emp_hy4")["ok"])
        self._wait_retries()
        with self.runner.lock:
            after = len([c for c in self.runner.calls if ":deliver:" in c["key"]])
        self.assertEqual(after, before + 1)
        task = self._task(result["task_id"])
        self.assertEqual(task["review_generation"], 2)
        self.assertEqual(task["auto_deliver_generation"], 2)
        self.assertEqual(task["deliver_source"], "review_auto")
        self.assertFalse(task.get("deliver_stale"))
        self.assertFalse(task.get("auto_deliver_inflight"))

    def test_failed_retry_review_does_not_supplement_delivery(self):
        # 审核还是失败 = 没结论：不自动补交付，也不会把上一版终稿误标成自动重出
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        self._wait(result["task_id"])
        with self.runner.lock:
            before = len([c for c in self.runner.calls if ":deliver:" in c["key"]])
        self.runner.fail_for = [":review:emp_ds4"]
        self.manager.retry("s1", result["task_id"], T.STAGE_REVIEW, "emp_ds4")
        self._wait_retries()
        with self.runner.lock:
            self.assertEqual(
                len([c for c in self.runner.calls if ":deliver:" in c["key"]]), before
            )
        task = self._task(result["task_id"])
        self.assertEqual(
            next(i["status"] for i in task["part_b"] if i["member_id"] == "emp_ds4"),
            T.STATUS_FAILED,
        )
        self.assertNotEqual(task.get("deliver_source"), "review_auto")
        self.assertNotEqual(task.get("auto_deliver_generation"), task.get("review_generation"))

    def test_manual_regeneration_replaces_auto_badge(self):
        # 自动补全之后再手点「重新生成交付」：来源回到手动，卡片不再挂自动标签
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        self._wait(result["task_id"])
        self.manager.retry("s1", result["task_id"], T.STAGE_REVIEW, "emp_ds4")
        self._wait_retries()
        self.assertEqual(self._task(result["task_id"])["deliver_source"], "review_auto")
        self.manager.retry("s1", result["task_id"], T.STAGE_DELIVER)
        self._wait_retries()
        task = self._task(result["task_id"])
        self.assertEqual(task["deliver_source"], "manual")
        self.assertEqual(task["part_a"]["final_source"], "manual")

    def test_new_task_card_exposes_creation_time(self):
        # 之后创建的任务：卡片数据里带 created_at，前端头部直接显示
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        self._wait(result["task_id"])
        session = self.store.get_session("s1")
        card_msg = next(m for m in session["messages"] if m.get("kind") == "task_card")
        self.assertTrue(card_msg["card"]["created_at"])
        self.assertEqual(
            card_msg["card"]["created_at"], self._task(result["task_id"])["created_at"]
        )

    def test_history_records_preserved_after_retry(self):
        # 取消 / 重试产生新记录，旧记录保留且完整输入 / 原始输出可查
        result = self._submit("/collab @agy @DS4 写方案")
        self._wait(result["task_id"])
        session = self.store.get_session("s1")
        first = [m for m in session["messages"] if m.get("kind") == "task_record"]
        self.assertEqual(len(first), 3)  # 执行 + 审核 + 交付
        self.assertTrue(all(m["sent_prompt"] and m["raw_response"] for m in first))

        self.manager.retry("s1", result["task_id"], T.STAGE_DELIVER)
        deadline = time.time() + 6
        while time.time() < deadline:
            session = self.store.get_session("s1")
            if len([m for m in session["messages"] if m.get("kind") == "task_record"]) >= 4:
                break
            time.sleep(0.02)
        records = [m for m in session["messages"] if m.get("kind") == "task_record"]
        self.assertEqual(len(records), 4)
        self.assertEqual(len([m for m in records if m["stage"] == "deliver"]), 2)
        self.assertTrue(all(m["in_chat"] is False for m in records))
        self.assertTrue(all(m["sent_prompt"] and m["raw_response"] for m in records))
        self.manager.wait_idle()

    def test_retry_delivery_regenerates_final(self):
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        self._wait(result["task_id"])
        self.manager.retry("s1", result["task_id"], T.STAGE_DELIVER)
        deadline = time.time() + 6
        while time.time() < deadline:
            with self.runner.lock:
                delivers = len([c for c in self.runner.calls if ":deliver:" in c["key"]])
            if delivers >= 2:
                break
            time.sleep(0.02)
        with self.runner.lock:
            delivers = len([c for c in self.runner.calls if ":deliver:" in c["key"]])
        self.assertGreaterEqual(delivers, 2)
        task = self._task(result["task_id"])
        self.assertTrue(task["part_a"]["final"]["summary"])
        self.manager.wait_idle()


class TestRecoveryAndProtocol(TaskCase):
    def test_recover_marks_running_as_cancelled(self):
        session = self.store.get_session("s1")
        session["tasks"]["t_old"] = {
            "task_id": "t_old", "type": T.TYPE_SINGLE, "status": T.STATUS_RUNNING,
            "owner_id": "emp_agy", "card_msg_id": "",
            "flow": [], "part_a": {}, "part_b": [],
        }
        session["task_queues"]["emp_agy"] = ["t_old"]
        self.store.save_session(session)
        count = self.manager.recover_on_startup()
        self.assertEqual(count, 1)
        task = self._task("t_old")
        self.assertEqual(task["status"], T.STATUS_CANCELLED)
        self.assertTrue(any("重启" in f["text"] for f in task["flow"]))

    def test_old_sessions_get_task_fields(self):
        session = self.store.get_session("s1")
        del session["tasks"]
        del session["task_queues"]
        del session["active_task_id"]
        self.store.save_session(session)
        result = self._submit("@agy 做事")
        self.assertIn(result["task_id"], self.store.get_session("s1")["tasks"])
        self.manager.wait_idle()

    def test_prompt_includes_protocol_files(self):
        prompt = T.build_prompt(
            T.STAGE_REVIEW, "写方案", T.TYPE_COLLAB,
            artifacts=[{"path": "a.md", "summary": "正文", "bytes": 10}],
        )
        self.assertIn("AI-Meeting 协作流程", prompt)
        self.assertIn("协助审核成员", prompt)
        self.assertIn("写方案", prompt)
        self.assertIn("a.md", prompt)

    def test_plain_text_output_fallback(self):
        parsed = T.parse_output("一段没有 JSON 的纯文本")
        self.assertEqual(parsed["summary"], "一段没有 JSON 的纯文本")
        self.assertEqual(parsed["artifacts"], [])

    def test_body_plus_artifact_block_keeps_body_only(self):
        # 协议要求「正文 + 末尾登记块」：聊天里只该看到正文，登记块被摘掉
        text = (
            "做完了：把三段合并成一份方案。\n\n"
            "```json\n"
            '{"artifacts": [{"path": "docs/plan.md", "summary": "方案", "bytes": 120}],'
            ' "changes": "采纳了 DS4 的拆分建议"}\n'
            "```"
        )
        parsed = T.parse_output(text)
        self.assertEqual(parsed["summary"], "做完了：把三段合并成一份方案。")
        self.assertNotIn("```", parsed["summary"])
        self.assertNotIn('"artifacts"', parsed["summary"])
        self.assertEqual(parsed["artifacts"][0]["path"], "docs/plan.md")
        self.assertEqual(parsed["artifacts"][0]["bytes"], 120)
        self.assertEqual(parsed["changes"], "采纳了 DS4 的拆分建议")

    def test_whole_json_without_summary_is_still_readable(self):
        # 模型只回了一份没有 summary 的结构：也不能把花括号原样丢进聊天气泡
        parsed = T.parse_output('{"title": "阶段二内容", "artifacts": []}')
        self.assertEqual(parsed["summary"], "title：阶段二内容")
        self.assertFalse(parsed["summary"].strip().startswith("{"))

    def test_markdown_fence_with_prose_inside_is_parsed(self):
        text = '正文两行\n```\n{"summary": "围栏里的旧写法", "artifacts": []}\n```'
        parsed = T.parse_output(text)
        self.assertEqual(parsed["summary"], "正文两行")


class TestRegressions(TaskCase):
    """复核阶段发现并修掉的几处缺陷，防止回归。"""

    def _wait_status(self, tid, status, timeout=6):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if (self._task(tid) or {}).get("status") == status:
                return True
            time.sleep(0.02)
        return False

    def test_retry_single_reruns_single_stage(self):
        # 单任务重试必须走单任务路径：结果消息要更新，且不该多跑一次交付环节
        self.runner.fail_for = [":exec:"]
        result = self._submit("@agy 做一件事")
        tid = result["task_id"]
        self._wait(tid)
        self.runner.fail_for = []
        self.manager.retry("s1", tid, T.STAGE_EXEC)
        self._wait_calls(2)
        self.assertTrue(self._wait_status(tid, T.STATUS_DELIVERED))
        session = self.store.get_session("s1")
        msg = [m for m in session["messages"] if m.get("kind") == "task_result"][0]
        self.assertEqual(msg["status"], T.STATUS_DELIVERED)
        self.assertTrue(msg["content"])
        self.assertTrue(msg["raw_response"])
        with self.runner.lock:
            stages = [c["key"].split(":")[2] for c in self.runner.calls]
        self.assertEqual(stages, ["exec", "exec"])
        self.assertEqual(self._task(tid)["part_a"].get("final"), {})
        self.manager.wait_idle()

    def test_recover_cancels_inflight_stages(self):
        # 审核进行中服务重启：环节状态要一起转已取消，否则卡片上的位置既不能取消也不能重试
        gate = threading.Event()
        original = self.runner

        def gated(employee, prompt, session_id, workspace_dir):
            if ":review:" in session_id:
                original.gates.setdefault(session_id, gate)
            return original(employee, prompt, session_id, workspace_dir)

        self.manager.runner = gated
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        tid = result["task_id"]
        deadline = time.time() + 6
        while time.time() < deadline:
            task = self._task(tid)
            if any(i["status"] == T.MEMBER_IN_PROGRESS for i in task["part_b"]):
                break
            time.sleep(0.02)
        self.assertEqual(self.manager.recover_on_startup(), 1)
        task = self._task(tid)
        self.assertEqual(task["status"], T.STATUS_CANCELLED)
        # 执行环节早已完成，不该被改写；只有进行中的审核位置转已取消
        self.assertEqual(task["part_a"]["status"], T.STATUS_DONE)
        self.assertTrue(all(i["status"] == T.STATUS_CANCELLED for i in task["part_b"]))
        # 被中断的审核位置重新可重试（旧实现会一直卡在「进行中」，返回「当前环节不需要重试」）
        self.assertTrue(self.manager.retry("s1", tid, T.STAGE_REVIEW, "emp_ds4")["ok"])
        gate.set()
        self.manager.wait_idle()

    def test_cancel_queued_task_syncs_message(self):
        # 排队中被取消的单任务：消息不能永远停在「排队中」，队列里也不该留死条目
        gate = threading.Event()
        original = self.runner

        def gated(employee, prompt, session_id, workspace_dir):
            original.gates.setdefault(session_id, gate)
            return original(employee, prompt, session_id, workspace_dir)

        self.manager.runner = gated
        r1 = self._submit("@agy 第一件")
        r2 = self._submit("@agy 第二件")
        self._wait_calls(1)
        self.assertTrue(self.manager.cancel("s1", r2["task_id"], T.STAGE_EXEC)["ok"])
        session = self.store.get_session("s1")
        msg = [m for m in session["messages"]
               if m.get("kind") == "task_result" and m.get("task_id") == r2["task_id"]][0]
        self.assertEqual(msg["status"], T.STATUS_CANCELLED)
        self.assertEqual(session["task_queues"]["emp_agy"], [r1["task_id"]])
        # 排队中被取消：没有进程可杀，也不该在进程级集合里留一个没人消费的标记
        self.assertNotIn(f"s1:{r2['task_id']}:exec:emp_agy", T.CANCELLED_KEYS)
        gate.set()
        self._wait(r1["task_id"])
        self.manager.wait_idle()

    def test_review_and_queue_task_do_not_overlap(self):
        # 同一成员串行：成员在审核时，派给它的队列任务要等审核结束再执行
        gate = threading.Event()
        original = self.runner

        def gated(employee, prompt, session_id, workspace_dir):
            if ":review:emp_ds4" in session_id:
                original.gates.setdefault(session_id, gate)
            return original(employee, prompt, session_id, workspace_dir)

        self.manager.runner = gated
        r1 = self._submit("/collab @agy @DS4 写方案")
        self._wait_calls(2)  # 主创执行 + DS4 审核都已进入
        r2 = self._submit("@DS4 另外做一份")
        time.sleep(0.3)
        with self.runner.lock:
            ds4_exec = [c for c in self.runner.calls
                        if ":exec:emp_ds4" in c["key"]]
        self.assertEqual(ds4_exec, [])  # 审核还没结束，队列任务不得开跑
        gate.set()
        self.assertEqual(self._wait(r1["task_id"])["status"], T.STATUS_DELIVERED)
        self.assertEqual(self._wait(r2["task_id"])["status"], T.STATUS_DELIVERED)
        self.manager.wait_idle()


class TestPromptContext(unittest.TestCase):
    """会话背景：最近若干条 + 更早的进展摘要，不进系统播报与环节记录。"""

    def _msg(self, i, sender, content, **kw):
        data = {
            "id": f"m_{i}", "sender": sender, "sender_name": sender,
            "content": content, "in_chat": True,
        }
        data.update(kw)
        return data

    def test_recent_messages_fill_context(self):
        messages = [self._msg(i, "user" if i % 2 == 0 else "emp_agy", f"第 {i} 条") for i in range(12)]
        context = T.build_context(messages)
        self.assertEqual(len(context["messages"]), T.CONTEXT_MAX_MSGS)
        self.assertEqual(context["messages"][-1]["content"], "第 11 条")
        self.assertEqual(context["dropped"], 4)
        self.assertTrue(context["summary"])

    def test_context_skips_system_and_records(self):
        messages = [
            self._msg(1, "user", "背景一"),
            self._msg(2, "system", "服务重启", kind="task_card"),
            self._msg(3, "emp_ds4", "环节记录", in_chat=False, kind="task_record"),
            self._msg(4, "user", "背景二"),
        ]
        context = T.build_context(messages)
        contents = [m["content"] for m in context["messages"]]
        self.assertEqual(contents, ["背景一", "背景二"])

    def test_current_message_is_not_duplicated(self):
        messages = [self._msg(1, "user", "上一句"), self._msg(2, "user", "这一句")]
        context = T.build_context(messages, "m_2")
        self.assertEqual([m["content"] for m in context["messages"]], ["上一句"])

    def test_prompt_contains_context_section(self):
        context = T.build_context([
            self._msg(1, "user", "先看这段背景"),
            self._msg(2, "emp_agy", "背景的结论是 X"),
        ])
        prompt = T.build_prompt(T.STAGE_EXEC, "接着做", T.TYPE_SINGLE, context=context)
        self.assertIn("【会话背景", prompt)
        self.assertIn("先看这段背景", prompt)
        self.assertIn("背景的结论是 X", prompt)
        # 用户原话仍然单独成段
        self.assertIn("【用户原话】\n接着做", prompt)


class TestBusinessInvariants(TaskCase):
    """按业务不变量回归（business-code-review）：

    I2 同一成员任意时刻只有一个进程；I3 终态不可逆；I5 不存在无法脱身的停留态；
    I6 任务 / 消息 / 卡片口径一致；I7 交付只在执行成功之后。
    """

    def test_crossed_collab_tasks_do_not_deadlock(self):
        # A：主创 agy / 审核 DS4；B：主创 DS4 / 审核 agy —— 早期实现会互等对方占用的成员
        r1 = self._submit("/collab @agy @DS4 做方案A")
        r2 = self._submit("/collab @DS4 @agy 做方案B")
        t1 = self._wait(r1["task_id"], timeout=8)
        t2 = self._wait(r2["task_id"], timeout=8)
        self.assertEqual(t1["status"], T.STATUS_DELIVERED)
        self.assertEqual(t2["status"], T.STATUS_DELIVERED)
        self.assertTrue(all(v <= 1 for v in self.runner.max_per_member.values()),
                        self.runner.max_per_member)
        self.manager.wait_idle()

    def test_cancel_is_rejected_on_finished_task(self):
        result = self._submit("/collab @agy @DS4 写方案")
        self._wait(result["task_id"])
        out = self.manager.cancel("s1", result["task_id"], T.STAGE_EXEC)
        self.assertEqual(out.get("code"), "task_finished")
        task = self._task(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_DELIVERED)
        self.assertEqual(task["part_a"]["status"], T.STATUS_DONE)
        self.assertTrue(task["artifacts"])

    def test_cancel_failed_task_keeps_failure(self):
        self.runner.fail_for = [":exec:"]
        result = self._submit("@agy 做一件事")
        self._wait(result["task_id"])
        out = self.manager.cancel("s1", result["task_id"], T.STAGE_EXEC)
        self.assertEqual(out.get("code"), "task_finished")
        task = self._task(result["task_id"])
        msg = [m for m in self.store.get_session("s1")["messages"]
               if m.get("kind") == "task_result"][0]
        self.assertEqual(task["status"], T.STATUS_FAILED)
        self.assertEqual(msg["status"], T.STATUS_FAILED)  # 三处口径一致

    def test_deliver_retry_requires_successful_exec(self):
        self.runner.fail_for = [":exec:"]
        result = self._submit("/collab @agy @DS4 写方案")
        self._wait(result["task_id"])
        out = self.manager.retry("s1", result["task_id"], T.STAGE_DELIVER)
        self.assertIn("error", out)
        self.assertEqual(self._task(result["task_id"])["status"], T.STATUS_FAILED)
        with self.runner.lock:
            self.assertEqual(len([c for c in self.runner.calls if ":deliver:" in c["key"]]), 0)

    def test_single_task_closes_part_a_and_recover_ignores_finished(self):
        result = self._submit("@agy 做一件事")
        task = self._wait(result["task_id"])
        self.assertEqual(task["part_a"]["status"], T.STATUS_DELIVERED)
        self.manager.recover_on_startup()
        task = self._task(result["task_id"])
        self.assertEqual(task["part_a"]["status"], T.STATUS_DELIVERED)
        self.assertFalse(any("服务重启" in f["text"] for f in task["flow"]))

    def test_retry_carries_later_user_constraints(self):
        self.runner.fail_for = [":exec:"]
        result = self._submit("@agy 写一份季度总结")
        self._wait(result["task_id"])
        self._submit("补充：数据用 2026 Q3 的")  # 没点名：只作为上下文保留
        self.runner.fail_for = []
        self.assertTrue(self.manager.retry("s1", result["task_id"], T.STAGE_EXEC)["ok"])
        self._wait(result["task_id"])
        with self.runner.lock:
            prompt = [c for c in self.runner.calls if ":exec:" in c["key"]][-1]["prompt"]
        self.assertIn("2026 Q3", prompt)
        self.assertIn("【用户原话】\n@agy 写一份季度总结", prompt)  # 原话不重复进背景

    def test_cancel_flag_lives_only_for_one_attempt(self):
        gate = threading.Event()
        original = self.runner

        def gated(employee, prompt, session_id, workspace_dir):
            original.gates.setdefault(session_id, gate)
            return original(employee, prompt, session_id, workspace_dir)

        self.manager.runner = gated
        result = self._submit("@agy 做一件事")
        self._wait_calls(1)
        self.manager.cancel("s1", result["task_id"], T.STAGE_EXEC)
        gate.set()
        self.manager.wait_idle()  # 等这次执行真正收尾（清理标记与落库在同一段）
        self._wait(result["task_id"])
        self.assertNotIn(f"s1:{result['task_id']}:exec:emp_agy", T.CANCELLED_KEYS)

    def test_member_wait_timeout_fails_instead_of_running_concurrently(self):
        # DS4 手上有长任务，另一任务的审核需要 DS4：等不到就标失败可重试，绝不并发开两个进程
        gate = threading.Event()
        original = self.runner

        def gated(employee, prompt, session_id, workspace_dir):
            if ":exec:emp_ds4" in session_id:
                original.gates.setdefault(session_id, gate)
            return original(employee, prompt, session_id, workspace_dir)

        self.manager.runner = gated
        self.manager.MEMBER_WAIT_SECONDS = 0.6
        long_task = self._submit("@DS4 长任务")
        self._wait_calls(1)
        reviewed = self._submit("/collab @agy @DS4 写方案")
        deadline = time.time() + 6
        while time.time() < deadline:
            item = next(i for i in self._task(reviewed["task_id"])["part_b"]
                        if i["member_id"] == "emp_ds4")
            if item["status"] == T.STATUS_FAILED:
                break
            time.sleep(0.02)
        item = next(i for i in self._task(reviewed["task_id"])["part_b"]
                    if i["member_id"] == "emp_ds4")
        self.assertEqual(item["status"], T.STATUS_FAILED)
        self.assertIn("等待该成员空闲超时", item["summary"])
        self.assertTrue(all(v <= 1 for v in self.runner.max_per_member.values()),
                        self.runner.max_per_member)
        gate.set()
        self._wait(long_task["task_id"])
        self._wait(reviewed["task_id"])
        self.manager.wait_idle()


    def test_timeout_is_terminal_and_blocks_cancel(self):
        # 超时是任务终态（设计文档 6.3）：不能被「取消」打回已取消，但可以重试
        self.runner.timeout_for = [":exec:"]
        result = self._submit("@agy 做一件事")
        task = self._wait(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_TIMEOUT)
        self.assertIn(task["status"], T.FINAL_TASK_STATES)
        self.assertEqual(task["part_a"]["status"], T.STATUS_TIMEOUT)
        rejected = self.manager.cancel("s1", result["task_id"], T.STAGE_EXEC)
        self.assertEqual(rejected.get("code"), "task_finished")
        self.assertEqual(self._task(result["task_id"])["status"], T.STATUS_TIMEOUT)
        self.runner.timeout_for = []
        self.assertTrue(self.manager.retry("s1", result["task_id"], T.STAGE_EXEC)["ok"])
        self.assertEqual(self._wait(result["task_id"])["status"], T.STATUS_DELIVERED)
        self.manager.wait_idle()

    def test_prompt_is_readable_while_stage_is_running(self):
        # 环节进行中点开「查看输入 / 原始输出」时应当已经能看到本轮 Prompt：
        # 早期实现要等执行结束才写，进行中查到的是空，UI 只能显示「未留存」
        self.manager.auto_start = False
        result = self._submit("/collab @agy @DS4 写方案")
        tid = result["task_id"]
        exec_key = T.execution_key("s1", tid, T.STAGE_EXEC, "emp_agy")
        review_key = T.execution_key("s1", tid, T.STAGE_REVIEW, "emp_ds4")
        gate = threading.Event()
        self.runner.gates[exec_key] = gate

        self.manager.ensure_worker("s1", "emp_agy")
        deadline = time.time() + 5
        while time.time() < deadline:
            if self._task(tid)["part_a"]["status"] == T.MEMBER_IN_PROGRESS:
                break
            time.sleep(0.02)
        task = self._task(tid)
        self.assertEqual(task["part_a"]["status"], T.MEMBER_IN_PROGRESS)
        self.assertIn("写方案", task["part_a"]["sent_prompt"])
        self.assertTrue(task["part_a"]["msg_id"], "进行中也应指向本轮执行记录")

        # 主创放行前先给审核换一个新闸门：同一个 Event 一旦 set，后面就不会再拦住
        review_gate = threading.Event()
        self.runner.gates[review_key] = review_gate
        gate.set()
        deadline = time.time() + 6
        while time.time() < deadline:
            item = (self._task(tid).get("part_b") or [{}])[0]
            if item.get("status") == T.MEMBER_IN_PROGRESS:
                break
            time.sleep(0.02)
        item = (self._task(tid).get("part_b") or [{}])[0]
        self.assertEqual(item.get("status"), T.MEMBER_IN_PROGRESS)
        self.assertTrue(item.get("sent_prompt"), "审核进行中也应能看到本轮 Prompt")
        review_gate.set()
        self.manager.wait_idle()

    def test_rerun_exec_failure_does_not_keep_previous_round(self):
        # 首轮主创成功、交付失败 → 任务 failed；再重跑执行且这轮执行也失败
        self.runner.fail_for = [":deliver:"]
        result = self._submit("/collab @agy @DS4 写方案")
        task = self._wait(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_FAILED)
        self.assertTrue(task["part_a"]["brief"])
        self.assertTrue(task["part_a"]["artifacts"])
        self.assertTrue(any(i["summary"] for i in task["part_b"]))  # 上一轮 DS4 意见
        with self.runner.lock:
            before = len(self.runner.calls)
        self.runner.fail_for = [":exec:"]
        self.assertTrue(self.manager.retry("s1", result["task_id"], T.STAGE_EXEC)["ok"])
        deadline = time.time() + 6
        while time.time() < deadline:
            with self.runner.lock:
                ran = len(self.runner.calls) > before
            task = self._task(result["task_id"])
            if ran and task["part_a"]["status"] == T.STATUS_FAILED:
                break
            time.sleep(0.02)
        task = self._task(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_FAILED)
        self.assertEqual(task["part_a"]["status"], T.STATUS_FAILED)
        # 这一轮什么都没产出，就不该继续展示上一轮的简报与原始输出；
        # Prompt 例外：本轮请求确实发出去了，留着才查得出「到底让它做了什么」
        for field in ("brief", "raw_output"):
            self.assertEqual(task["part_a"][field], "", field)
        self.assertTrue(task["part_a"]["sent_prompt"], "失败的一轮也应保留本轮 Prompt")
        self.assertEqual(task["part_a"]["artifacts"], [])
        self.assertEqual(task["artifacts"], [])
        self.assertTrue(all(i["status"] == T.MEMBER_WAITING for i in task["part_b"]))
        self.assertTrue(all(not i["summary"] for i in task["part_b"]))
        self.manager.wait_idle()



    @classmethod
    def setUpClass(cls):
        import app as flask_app
        cls.app_module = flask_app
        cls.client = flask_app.app.test_client()
        cls.store = FakeStore(settings=make_settings())
        cls.runner = FakeRunner()
        storage.save_settings(cls.store.settings)  # 接口测试用同一份成员表
        cls.fake_manager = T.TaskManager(
            store=storage, runner=cls.runner,
            killer=lambda key: cls.runner.killed.add(key),
        )
        cls.app_module.task_manager = cls.fake_manager
        T.CANCELLED_KEYS.clear()

    def test_collab_validation_endpoint(self):
        bad = self.client.post("/api/collab/validate", json={"content": "/collab @agy 一个人"})
        self.assertEqual(bad.status_code, 200)
        self.assertFalse(bad.get_json()["ok"])
        good = self.client.post("/api/collab/validate", json={"content": "/collab @agy @DS4 写方案"})
        self.assertTrue(good.get_json()["ok"])

    def test_message_and_cancel_retry_endpoints(self):
        created = self.client.post("/api/sessions", json={"title": "接口测试"})
        self.assertEqual(created.status_code, 200)
        sid = created.get_json()["id"]
        self.assertIn("tasks", created.get_json())

        bad = self.client.post(f"/api/sessions/{sid}/message",
                               json={"content": "/collab @agy 一个人"})
        self.assertEqual(bad.status_code, 400)
        self.assertTrue(bad.get_json().get("validation_error"))

        sent = self.client.post(f"/api/sessions/{sid}/message",
                                json={"content": "@agy 做一件事"})
        self.assertEqual(sent.status_code, 200)
        session = sent.get_json()
        tid = session["active_task_id"]
        self.assertIsNotNone(tid)
        result_msg = [m for m in session["messages"] if m.get("kind") == "task_result"]
        self.assertEqual(len(result_msg), 1)

        deadline = time.time() + 6
        while time.time() < deadline:
            current = self.client.get(f"/api/sessions/{sid}").get_json()
            if current["tasks"][tid]["status"] == T.STATUS_DELIVERED:
                break
            time.sleep(0.02)

        # 已交付任务：取消必须被拒（终态不可逆），但「重新生成」要放行——重跑同一条任务
        cancelled = self.client.post(
            f"/api/sessions/{sid}/tasks/{tid}/cancel", json={"stage": "exec"}
        )
        self.assertEqual(cancelled.status_code, 409)
        self.assertEqual(cancelled.get_json().get("code"), "task_finished")
        runner = self.fake_manager.runner  # 接口测试里的执行器挂在注入给 app 的 manager 上
        with runner.lock:
            before = len([c for c in runner.calls if ":exec:" in c["key"]])
        retried = self.client.post(
            f"/api/sessions/{sid}/tasks/{tid}/retry", json={"stage": "exec"}
        )
        self.assertEqual(retried.status_code, 200)
        self.assertTrue(retried.get_json().get("ok"))
        # 重新生成跑的是同一个任务、同一条消息，不新增任务
        deadline = time.time() + 6
        execs = before
        while time.time() < deadline:
            with runner.lock:
                execs = len([c for c in runner.calls if ":exec:" in c["key"]])
            current = self.client.get(f"/api/sessions/{sid}").get_json()
            if execs == before + 1 and current["tasks"][tid]["status"] == T.STATUS_DELIVERED:
                break
            time.sleep(0.02)
        self.assertEqual(execs, before + 1)
        self.assertEqual(len(current["tasks"]), 1)
        self.assertEqual(len([m for m in current["messages"] if m.get("kind") == "task_result"]), 1)


class TestReviewStandard(TaskCase):
    """审核规程四条标准 + [通过] 判定从模型输出一路传到卡片与主创。"""

    PASS_OUTPUT = (
        "[通过] 结构与用户原话逐条对应，边界情形都写了兜底，无需改动。\n"
        "核对范围：out.md\n"
        "```json\n"
        '{"artifacts":[{"path":"out.md","summary":"终版"}],'
        '"review":{"verdict":"pass","issues":0,"ideas":0}}\n'
        "```"
    )
    ISSUES_OUTPUT = (
        "[意见] 1 处隐患、1 条巧思。\n"
        "核对范围：out.md\n"
        "## 隐患与漏洞\n"
        "- out.md:第 3 节 失败路径没给兜底 → 依赖缺失时走不下去 → 补一条降级步骤\n"
        "## 新意与巧思\n"
        "- out.md:第 5 节 用查表替代逐级判断，省一半步骤；代价是要先维护一张表（可选）\n"
        "```json\n"
        '{"artifacts":[{"path":"out.md","summary":"终版"}],'
        '"review":{"verdict":"issues","issues":1,"ideas":1}}\n'
        "```"
    )

    def test_protocol_contains_four_standards_and_no_stale_bans(self):
        text = T._read_protocol("roles/reviewer.md")
        for kw in ("审视隐患与漏洞", "逻辑自洽性", "边界条件", "实操可用性",
                   "新意与巧思", "篇幅不设上限", "[通过]",
                   "严禁为了提意见而硬挤琐碎细节", "隐患与漏洞"):
            self.assertIn(kw, text)
        # 这两条会把「尚未落盘的巧思」一起禁掉，不能回来
        self.assertNotIn("不要要求用户没提过的新功能", text)
        self.assertNotIn("不要对未落盘的设想发表意见", text)
        self.assertNotIn("不对未落盘的设想发表意见",
                         T._read_protocol("protocol/task-flow.md"))

    def test_parse_output_strips_registry_and_reads_verdict(self):
        parsed = T.parse_output(self.PASS_OUTPUT)
        self.assertTrue(parsed["summary"].startswith("[通过]"))
        self.assertNotIn("```json", parsed["summary"])
        self.assertEqual(parsed["review"]["verdict"], "pass")
        self.assertEqual(len(parsed["artifacts"]), 1)
        self.assertEqual(
            T.parse_output(self.ISSUES_OUTPUT)["review"],
            {"verdict": "issues", "issues": 1, "ideas": 1},
        )

    def test_parse_output_infers_verdict_from_first_line(self):
        # 模型漏写登记块时也要能判定「通过」
        self.assertEqual(T.parse_output("[通过] 已经够好。")["review"]["verdict"], "pass")
        self.assertEqual(T.parse_output("[意见] 有一处硬伤。")["review"]["verdict"], "issues")
        self.assertEqual(T.parse_output("普通执行结果")["review"], {})

    def test_verdict_not_trusted_when_invalid(self):
        bad = '{"review":{"verdict":"maybe","issues":9}}'
        self.assertEqual(T.parse_output("```json\n" + bad + "\n```")["review"], {})

    def test_build_prompt_marks_pass_for_owner(self):
        prompt = T.build_prompt(
            T.STAGE_DELIVER, "写方案", T.TYPE_COLLAB,
            opinions=[{"name": "DS4", "summary": "[通过] 够好", "review_verdict": "pass"}],
        )
        self.assertIn("[通过]，无需改动", prompt)
        # 老会话没有 verdict：不加标记，也不能报错
        old = T.build_prompt(
            T.STAGE_DELIVER, "写方案", T.TYPE_COLLAB,
            opinions=[{"name": "HY4", "summary": "一条意见"}],
        )
        self.assertIn("- HY4: 一条意见", old)

    def test_pass_review_reaches_card_and_owner(self):
        self.runner.outputs[":review:"] = self.PASS_OUTPUT
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        self._wait(result["task_id"])
        self.manager.wait_idle()
        task = self._task(result["task_id"])
        for item in task["part_b"]:
            self.assertEqual(item["status"], T.STATUS_DONE)
            self.assertEqual(item["review_verdict"], "pass")
            self.assertTrue(item["summary"].startswith("[通过]"))
            self.assertNotIn("```json", item["summary"])
        self.assertTrue(any("审核通过" in f["text"] for f in task["flow"]))
        card = T.build_card(task)
        self.assertTrue(all(i["review_verdict"] == "pass" for i in card["part_b"]))
        with self.runner.lock:
            deliver_prompts = [c["prompt"] for c in self.runner.calls
                               if ":deliver:" in c["key"]]
        self.assertTrue(deliver_prompts)
        self.assertTrue(all("[通过]，无需改动" in p for p in deliver_prompts))

    def test_issues_review_has_no_pass_label(self):
        self.runner.outputs[":review:"] = self.ISSUES_OUTPUT
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        self._wait(result["task_id"])
        self.manager.wait_idle()
        task = self._task(result["task_id"])
        for item in task["part_b"]:
            self.assertEqual(item["review_verdict"], "issues")
            self.assertEqual(item["review_counts"], {"issues": 1, "ideas": 1})
        self.assertFalse(any("审核通过" in f["text"] for f in task["flow"]))

    LEGACY_REVIEW_PROMPT = (
        "# 角色规程 · 协助审核成员\n要求：\n"
        "- 没有意见时正文明确写「无意见」，软件会在卡片里如实标记。\n"
        "- 阅读输入里列出的真实产物。\n"
    )

    def test_current_review_prompt_has_four_standards_and_review_registry(self):
        """输入快照是每次现拼的：新规程四条标准 + 审核专用登记要求都在里面。"""
        prompt = T.build_prompt(T.STAGE_REVIEW, "写方案", T.TYPE_COLLAB)
        for kw in ("审视隐患与漏洞", "新意与巧思", "精简实用", "达标即过",
                   "[通过]", "[意见]", '"review"', "即使没有产出文件也要写这一段"):
            self.assertIn(kw, prompt)
        # 流程概述与旧角色规程的措辞都不能再混进输入
        for kw in ("独立给出审核意见或优化建议", "没有意见时正文明确写「无意见」",
                   "不对未落盘的设想发表意见", "不要对未落盘的设想发表意见"):
            self.assertNotIn(kw, prompt)
        # 执行 / 交付段仍用原来的输出要求，没被审核专用文案带偏
        for stage in (T.STAGE_EXEC, T.STAGE_DELIVER):
            other = T.build_prompt(stage, "写方案", T.TYPE_COLLAB)
            self.assertIn("如果产出了文件", other)
            self.assertNotIn("即使没有产出文件也要写这一段", other)

    def test_retry_review_rebuilds_prompt_with_current_protocol(self):
        """存量会话里挂着旧规程快照时，点「重新审核」不会复用它，而是重新拼一份。"""
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        self._wait(result["task_id"])
        self.manager.wait_idle()
        tid = result["task_id"]
        with self.store.session_lock("s1"):
            session = self.store.get_session("s1")
            task = session["tasks"][tid]
            member = task["part_b"][0]["member_id"]
            task["part_b"][0]["sent_prompt"] = self.LEGACY_REVIEW_PROMPT
            task["part_b"][0]["review_verdict"] = "pass"
            task["part_b"][0]["review_counts"] = {"issues": 0, "ideas": 0}
            self.store.save_session(session)

        retry = self.manager.retry("s1", tid, T.STAGE_REVIEW, member)
        self.assertTrue(retry.get("ok"), retry)
        self._wait_retries()
        self.manager.wait_idle()

        item = next(i for i in self._task(tid)["part_b"]
                    if i["member_id"] == member)
        fresh = item["sent_prompt"]
        self.assertIn("审视隐患与漏洞", fresh)
        self.assertIn("达标即过", fresh)
        self.assertNotIn("没有意见时正文明确写「无意见」", fresh)
        # 旧判定也随这一轮重审清掉重算，不会把上一轮的 pass 带过来
        self.assertNotEqual(item.get("review_verdict"), "pass")
        # 真正发给 CLI 的那份输入同样是新规程
        with self.runner.lock:
            calls = [c["prompt"] for c in self.runner.calls if ":review:" in c["key"]]
        self.assertGreaterEqual(len(calls), 3)
        self.assertTrue(all("审视隐患与漏洞" in p for p in calls))


class TestReviewSemanticRetry(TaskCase):
    """模型只回「我先读一下…」这类开场白时：语义层重试 + 判无意见 + 气泡文案正确。

    回归自 17d6e8a 的旧修复：任务系统重构时把 run_with_retry 丢了，
    导致 hy4-std 这种「说了要做却没给结论」的回复被当成「完成」。
    """

    def test_detector_recognizes_intent_only_replies(self):
        from core.executor import looks_like_incomplete as L
        # 真实回归样本：hy4-std 的英文开场白（S 号：6373f3ce / t_60f4c7b0）
        self.assertTrue(L("I'll read the actual artifact and surrounding context before judging."))
        self.assertTrue(L("我先读取一下产物和上下文再判断。"))
        self.assertTrue(L("Let me read the files first."))
        # 正常的短结论不算残缺
        self.assertFalse(L("[通过] 内容扎实，无硬伤。"))
        self.assertFalse(L("[意见] 有 2 处隐患。\n核对范围：A01.md"))

    def test_preamble_only_review_is_retried_then_no_opinion(self):
        preamble = "I'll read the actual artifact and surrounding context before judging."
        self.runner.outputs = {":review:emp_ds4": preamble}
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        task = self._wait(result["task_id"])
        item = next(i for i in task["part_b"] if i["member_id"] == "emp_ds4")
        # 不能冒充「完成」，记「无意见」
        self.assertEqual(item["status"], T.STATUS_NO_OPINION)
        self.assertEqual(item["review_verdict"], "")
        # 气泡正文换成明确说明，不把开场白当结论
        self.assertIn("未给出有效结论", item["summary"])
        self.assertNotIn("I'll read the actual artifact", item["summary"])
        # 原始开场白仍留在 raw_output 里可查
        self.assertIn("I'll read the actual artifact", item["raw_output"])
        # 确实做了语义层重试
        with self.runner.lock:
            calls = [c for c in self.runner.calls if ":review:emp_ds4" in c["key"]]
        self.assertGreaterEqual(len(calls), 2)
        # 无效结论不会作为审核意见喂给主创
        deliver_prompt = next(c["prompt"] for c in self.runner.calls if ":deliver:" in c["key"])
        self.assertNotIn("I'll read the actual artifact", deliver_prompt)
        self.manager.wait_idle()

    def test_preamble_then_valid_review_uses_retried_answer(self):
        # 第一次开场白、第二次给出正常结论：重试后采用后者，不吞掉真实审核
        self.runner.sequence_outputs = {
            ":review:emp_ds4": [
                "I'll read the actual artifact and surrounding context before judging.",
                "[意见] 有一处隐患。\n核对范围：A01.md\n```json\n"
                '{"review":{"verdict":"issues","issues":1,"ideas":0}}\n```',
            ],
        }
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        task = self._wait(result["task_id"])
        item = next(i for i in task["part_b"] if i["member_id"] == "emp_ds4")
        self.assertEqual(item["status"], T.STATUS_DONE)
        self.assertEqual(item["review_verdict"], "issues")
        self.assertIn("有一处隐患", item["summary"])
        self.manager.wait_idle()

    def test_substantive_review_without_marker_is_not_retried(self):
        # 内容充实、只是忘了首行标记：不算开场白，不重试、不误判无意见
        long_review = (
            "核对范围：A01.md。这份文档整体可用，但第三节与第七节在屏幕使用上口径冲突，"
            "建议在第三节补一句前提限定，并同步第七节的示例，避免家长照做时左右为难。"
        ) * 3
        self.runner.outputs = {":review:emp_ds4": long_review}
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        task = self._wait(result["task_id"])
        item = next(i for i in task["part_b"] if i["member_id"] == "emp_ds4")
        self.assertEqual(item["status"], T.STATUS_DONE)
        with self.runner.lock:
            calls = [c for c in self.runner.calls if ":review:emp_ds4" in c["key"]]
        self.assertEqual(len(calls), 1)  # 没有语义重试
        self.manager.wait_idle()


class TestRunningCardClock(TaskCase):
    """卡片「进行中」必须同时有开始时刻和实时计时。

    历史表现：只有等环节跑完才写 started_at，跑了多久、几点开始全靠猜；
    单条重审还会沿用整条任务最早的 started_at，看起来像跑了半小时。
    """

    def test_review_item_has_started_at_while_running(self):
        self.runner.review_gate = threading.Event()
        result = self._submit("/collab @agy @DS4 写一份方案")
        tid = result["task_id"]
        # 等审核调用真的进来（被门挡住，此刻状态已经是「进行中」）
        deadline = time.time() + 5
        while time.time() < deadline:
            if any(":review:" in c["key"] for c in self.runner.calls):
                break
            time.sleep(0.02)
        item = [i for i in self._task(tid)["part_b"] if i["member_id"] == "emp_ds4"][0]
        self.assertEqual(item["status"], T.MEMBER_IN_PROGRESS)
        self.assertTrue(item["started_at"], "审核进行中就要有开始时刻，卡片才能计时")
        self.assertIsNone(item["finished_at"])
        self.runner.review_gate.set()
        self.manager.wait_idle()

    def test_retry_review_resets_card_clock(self):
        result = self._submit("/collab @agy @DS4 写一份方案")
        tid = result["task_id"]
        self._wait(tid)

        # 把卡片计时改成「很久以前开始」，模拟已交付很久之后再点重新审核
        task = self._task(tid)
        task["started_at"] = 1000
        task["finished_at"] = 0

        out = self.manager.retry("s1", tid, T.STAGE_REVIEW, "emp_ds4")
        self.assertTrue(out.get("ok"))
        task = self._task(tid)
        self.assertGreater(task["started_at"], 1000, "重审应从点下去的那一刻重新计时")
        self.manager.wait_idle()
        self._wait_retries()


class TestDefaultRouteToLastAi(TaskCase):
    """没 @ 任何人的消息，默认接着「最近一位回过话的成员」说。

    起因：聊天窗里连着追问（「再补一句」「按刚才的改」）很常见，每句都要求
    重新打一遍 @ 谁，实际用起来很别扭。默认对象只取本场会话里现存且可用的成员，
    一个都没有时仍然只把消息当上下文保留（不凭空建任务）。
    """

    def _seed(self, senders, settings=None):
        session = self.store.get_session("s1")
        for i, sender in enumerate(senders):
            session["messages"].append({
                "id": f"m_seed_{i}", "sender": sender, "sender_name": sender,
                "content": f"第 {i} 条历史", "in_chat": True, "created_at": 100 + i,
            })
        self.store.save_session(session)
        if settings is not None:
            self.settings = settings
            self.store.settings = settings

    def test_no_mention_goes_to_last_ai(self):
        self.manager.auto_start = False
        # 最后一条是 HY4 说的；更早是 agy
        self._seed(["emp_agy", "user", "emp_hy4"])
        result = self.manager.submit_message("s1", "再补一句结论", self.settings)
        self.assertIn("task_id", result, result)
        task = self._task(result["task_id"])
        self.assertEqual(task["owner_id"], "emp_hy4")
        user_msg = [m for m in self.store.get_session("s1")["messages"] if m["sender"] == "user"][-1]
        # 前端据此在气泡里标「默认发给 XXX」，避免「我没点名它怎么回了」
        self.assertEqual(user_msg["auto_to"]["id"], "emp_hy4")
        self.assertEqual(user_msg["auto_to"]["name"], "HY4")
        self.manager.wait_idle()

    def test_mention_still_wins_over_history(self):
        self.manager.auto_start = False
        self._seed(["emp_hy4"])
        result = self.manager.submit_message("s1", "@DS4 你看一下这个", self.settings)
        self.assertEqual(self._task(result["task_id"])["owner_id"], "emp_ds4")
        self.manager.wait_idle()

    def test_skips_disabled_member(self):
        self.manager.auto_start = False
        settings = copy.deepcopy(self.settings)
        for emp in settings["employees"]:
            if emp["id"] == "emp_hy4":
                emp["enabled"] = False
        self._seed(["emp_ds4", "emp_hy4"], settings=settings)
        result = self.manager.submit_message("s1", "继续说", settings)
        self.assertEqual(self._task(result["task_id"])["owner_id"], "emp_ds4")
        self.manager.wait_idle()

    def test_no_history_stays_context_only(self):
        self.manager.auto_start = False
        result = self.manager.submit_message("s1", "大家好", self.settings)
        self.assertEqual(result.get("info"), "context_only")
        self.assertNotIn("task_id", result)


class TestResumeAfterInterrupt(unittest.TestCase):
    """中断续跑：模型是新进程，上一轮的思路没了，只能靠「写了什么 + 改了哪些文件」接上。

    覆盖两件事：①失败/超时/取消时把这些物证留进 task["interrupt"]；
    ②下一轮 Prompt（重试 / 新指令）真的把它交到成员手里。
    """

    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="aim-resume-")
        self.store = FakeStore(make_session(workspace_dir=self.workspace))
        self.runner = FakeRunner()
        self.manager = T.TaskManager(store=self.store, runner=self.runner)
        self.settings = self.store.load_settings()
        T.CANCELLED_KEYS.clear()

    def tearDown(self):
        self.manager.wait_idle(timeout=5)
        T.CANCELLED_KEYS.clear()

    def _submit(self, content):
        return self.manager.submit_message("s1", content, self.settings)

    def _wait(self, tid, timeout=6):
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = self.store.get_session("s1")["tasks"].get(tid)
            if task and task.get("status") in (T.STATUS_DELIVERED, T.STATUS_CANCELLED,
                                               T.STATUS_FAILED, T.STATUS_TIMEOUT):
                return task
            time.sleep(0.02)
        return self.store.get_session("s1")["tasks"].get(tid)

    def _write(self, name, text="第二章已经写完"):
        path = os.path.join(self.workspace, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_prompt_section_carries_partial_and_files(self):
        prompt = T.build_prompt(
            T.STAGE_EXEC, "把第三章写完", T.TYPE_SINGLE,
            resume={
                "same_task": True, "status": T.STATUS_FAILED,
                "at": int(time.time()), "elapsed": 42,
                "partial": "第二章已写完，第三章开头两段如下…",
                "files": [{"path": "book.md", "bytes": 2048}],
            },
        )
        self.assertIn("【上次中断的进度】", prompt)
        self.assertIn("第三章开头两段", prompt)
        self.assertIn("book.md（2.0 KB）", prompt)
        self.assertIn("【续跑要求】", prompt)
        self.assertIn("不要重做", prompt)
        # 交接条排在「用户原话」之后：先看要做什么，再看做到哪
        self.assertLess(prompt.index("【用户原话】"), prompt.index("【上次中断的进度】"))

    def test_failure_records_interrupt_with_changed_files(self):
        self._write("book.md")
        self.runner.fail_for = [":exec:"]
        result = self._submit("@agy 把第三章写完")
        task = self._wait(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_FAILED)
        interrupt = task.get("interrupt") or {}
        self.assertEqual(interrupt.get("stage"), T.STAGE_EXEC)
        self.assertEqual(interrupt.get("status"), T.STATUS_FAILED)
        self.assertEqual([f["path"] for f in interrupt.get("files") or []], ["book.md"])

    def test_cancel_records_interrupt_too(self):
        self._write("book.md")
        gate = threading.Event()
        original = self.runner

        def gated(employee, prompt, session_id, workspace_dir):
            original.gates.setdefault(session_id, gate)
            return original(employee, prompt, session_id, workspace_dir)

        self.manager.runner = gated
        result = self._submit("@agy 把第三章写完")
        deadline = time.time() + 6
        while time.time() < deadline and not original.calls:
            time.sleep(0.01)
        self.manager.cancel("s1", result["task_id"], T.STAGE_EXEC)
        gate.set()
        # cancel() 是同步改状态的，但交接条由 worker 线程在进程返回后才写：
        # 只等「终态」会抢跑，这里等交接条落库
        deadline = time.time() + 5
        interrupt = {}
        while time.time() < deadline:
            interrupt = self.store.get_session("s1")["tasks"][result["task_id"]].get("interrupt") or {}
            if interrupt:
                break
            time.sleep(0.02)
        task = self._wait(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_CANCELLED)
        # 被取消同样算中断：下一轮重试也要知道半成品在哪
        self.assertEqual(interrupt.get("status"), T.STATUS_CANCELLED)
        self.assertTrue(interrupt.get("files"))

    def test_retry_prompt_tells_member_to_continue(self):
        self._write("book.md")
        self.runner.fail_for = [":exec:"]
        result = self._submit("@agy 把第三章写完")
        self._wait(result["task_id"])

        self.runner.fail_for = []
        self.manager.retry("s1", result["task_id"], T.STAGE_EXEC)
        task = self._wait(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_DELIVERED)

        second_prompt = self.runner.calls[-1]["prompt"]
        self.assertIn("【上次中断的进度】", second_prompt)
        self.assertIn("book.md", second_prompt)
        self.assertIn("【续跑要求】", second_prompt)
        # 跑成功之后交接条没用了，留着会让下次重试误以为没成
        self.assertNotIn("interrupt", task)

    def test_new_instruction_gets_handoff_without_old_answer(self):
        self._write("book.md")
        self.runner.fail_for = [":exec:"]
        first = self._submit("@agy 把第三章写完")
        self._wait(first["task_id"])

        self.runner.fail_for = []
        second = self._submit("@agy 接着上次的继续写")
        self._wait(second["task_id"])

        prompt = self.runner.calls[-1]["prompt"]
        self.assertIn("【上一条任务留下的半成品】", prompt)
        self.assertIn("book.md", prompt)
        # 换了任务：只给文件线索，不冒充「同一任务的上一轮」
        self.assertNotIn("【上次中断的进度】", prompt)
        self.assertNotIn("【续跑要求】", prompt)

    def test_success_leaves_no_interrupt(self):
        result = self._submit("@agy 写点东西")
        task = self._wait(result["task_id"])
        self.assertNotIn("interrupt", task)

    def test_handoff_ignores_other_members_and_stale_runs(self):
        """交接条只给「同一位成员 + 两小时内」的新指令，换人/太旧就不塞噪音。"""
        self._write("book.md")
        self.runner.fail_for = [":exec:"]
        first = self._submit("@agy 把第三章写完")
        self._wait(first["task_id"])

        # 手动把时间拨到 3 小时前：超过复用窗口
        session = self.store.get_session("s1")
        session["tasks"][first["task_id"]]["interrupt"]["at"] = int(time.time()) - 3 * 3600
        self.store.save_session(session)

        self.runner.fail_for = []
        second = self._submit("@DS4 换你看一下")
        self._wait(second["task_id"])
        prompt = self.runner.calls[-1]["prompt"]
        self.assertNotIn("【上一条任务留下的半成品】", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestFanout(TaskCase):
    """一条消息 @ 多位 → 并行分工：每位各一条单任务、各出一条回答。

    以前只认第一个 @，其余人静默收不到任何东西；现在要求「三份独立产出」。
    """

    def _tasks(self):
        return list(self.store.get_session("s1")["tasks"].values())

    def _results(self):
        return [m for m in self.store.get_session("s1")["messages"]
                if m.get("kind") == "task_result"]

    def test_three_mentions_create_three_tasks(self):
        result = self._submit("@agy @DS4 @HY4 请帮我整理一份去北京的攻略")
        self.assertEqual(result["type"], T.TYPE_SINGLE)
        self.assertEqual(len(result["task_ids"]), 3)
        self.assertTrue(result.get("fanout_id"))

        tasks = self._tasks()
        self.assertEqual(len(tasks), 3)
        self.assertEqual({t["owner_id"] for t in tasks}, {"emp_agy", "emp_ds4", "emp_hy4"})
        self.assertEqual({t["goal"] for t in tasks},
                         {"@agy @DS4 @HY4 请帮我整理一份去北京的攻略"})
        # 三条回答气泡，各自带 owner，互不覆盖
        self.assertEqual(len(self._results()), 3)
        self.assertEqual({m["sender"] for m in self._results()},
                         {"emp_agy", "emp_ds4", "emp_hy4"})

        for tid in result["task_ids"]:
            self._wait(tid)
        for tid in result["task_ids"]:
            task = self._task(tid)
            self.assertEqual(task["status"], T.STATUS_DELIVERED)
        self.assertTrue(all(t.get("fanout_id") == result["fanout_id"] for t in self._tasks()))

    def test_each_prompt_carries_own_file_prefix(self):
        result = self._submit("@agy @DS4 @HY4 整理北京攻略")
        for tid in result["task_ids"]:
            self._wait(tid)
        prompts = {t["owner_id"]: t["part_a"]["sent_prompt"] for t in self._tasks()}
        self.assertEqual(len(prompts), 3)
        expected = {"emp_agy": "agy", "emp_ds4": "DS4", "emp_hy4": "HY4"}
        for owner, prefix in expected.items():
            prompt = prompts[owner]
            self.assertIn("【并行分工】", prompt)
            self.assertIn(f"「{prefix}-」", prompt)
            # 同批其他成员要写清名字：模型才知道自己这份跟谁不重名
            for other in set(expected.values()) - {prefix}:
                self.assertIn(other, prompt)
        self.manager.wait_idle()

    def test_single_mention_keeps_single_task(self):
        result = self._submit("@agy 整理北京攻略")
        self.assertNotIn("task_ids", result)
        self.assertEqual(len(self._tasks()), 1)
        prompt = self._tasks()[0]["part_a"]["sent_prompt"]
        self.assertNotIn("【并行分工】", prompt)
        self.manager.wait_idle()

    def test_duplicate_mention_is_deduped(self):
        result = self._submit("@agy @agy 整理北京攻略")
        self.assertNotIn("task_ids", result)
        self.assertEqual(len(self._tasks()), 1)
        self.manager.wait_idle()

    def test_disabled_member_blocks_the_whole_fanout(self):
        settings = copy.deepcopy(self.settings)
        for emp in settings["employees"]:
            if emp["id"] == "emp_hy4":
                emp["enabled"] = False
        self.manager.auto_start = False
        result = self.manager.submit_message("s1", "@agy @DS4 @HY4 整理北京攻略", settings)
        self.assertTrue(result.get("validation_error"))
        self.assertIn("HY4", result["error"])
        # 少发一份比发错更难发现，所以宁可一条都不建
        self.assertEqual(self._tasks(), [])

    def test_one_member_failure_does_not_touch_siblings(self):
        self.runner.fail_for = [":exec:emp_ds4"]
        result = self._submit("@agy @DS4 @HY4 整理北京攻略")
        for tid in result["task_ids"]:
            self._wait(tid)
        statuses = {t["owner_id"]: t["status"] for t in self._tasks()}
        self.assertEqual(statuses["emp_agy"], T.STATUS_DELIVERED)
        self.assertEqual(statuses["emp_ds4"], T.STATUS_FAILED)
        self.assertEqual(statuses["emp_hy4"], T.STATUS_DELIVERED)

        # 重试只重跑失败那一位，另外两位不再被调用
        self.runner.fail_for = []
        T.CANCELLED_KEYS.clear()
        ds4_task = next(t for t in self._tasks() if t["owner_id"] == "emp_ds4")
        self.manager.retry("s1", ds4_task["task_id"], T.STAGE_EXEC)
        task = self._wait(ds4_task["task_id"])
        self.assertEqual(task["status"], T.STATUS_DELIVERED)
        calls = [(c["key"]) for c in self.runner.calls if ":exec:" in c["key"]]
        self.assertEqual(len([k for k in calls if "emp_ds4" in k]), 2)
        self.assertEqual(len([k for k in calls if "emp_agy" in k]), 1)
        self.assertEqual(len([k for k in calls if "emp_hy4" in k]), 1)

    def test_collab_still_takes_the_collab_path(self):
        result = self._submit("/collab @agy @DS4 @HY4 整理北京攻略")
        self.assertEqual(result["type"], T.TYPE_COLLAB)
        self.assertNotIn("task_ids", result)
        self.assertEqual(len(self._tasks()), 1)
        self.manager.wait_idle()

    def test_file_prefix_prefers_bracket_short_name(self):
        """文件名前缀用括号里的短名：`DeepSeek-V4 (DS4)` → `DS4-攻略.md`。"""
        self.assertEqual(T._safe_prefix("agy"), "agy")
        self.assertEqual(T._safe_prefix("DeepSeek-V4 (DS4)"), "DS4")
        self.assertEqual(T._safe_prefix("混元-4 （HY4）"), "HY4")
        # 路径分隔符、空白、冒号不能进文件名
        self.assertEqual(T._safe_prefix("a/b:c d"), "a-b-c-d")
        self.assertEqual(T._safe_prefix(""), "member")
