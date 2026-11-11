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

    def __call__(self, employee, prompt, session_id, workspace_dir):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
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
            gate = self.gates.get(session_id)
            if gate is not None:
                gate.wait(timeout=3)
            if session_id in self.killed:
                return SimpleNamespace(status="interrupted", output="", raw_output="", elapsed=0)
            if any(flag in session_id for flag in self.fail_for):
                return SimpleNamespace(status="error", output="调用失败", raw_output="调用失败", elapsed=0)
            if any(flag in session_id for flag in self.empty_for):
                return SimpleNamespace(status="success", output="", raw_output="", elapsed=0)
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

    def test_no_mention_is_context_only(self):
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
        self.runner.review_barrier = threading.Barrier(2)
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        self._wait_calls(3, timeout=6)
        self.manager.cancel("s1", result["task_id"], T.STAGE_REVIEW, "emp_ds4")
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
        result = self._submit("/collab @agy @DS4 @HY4 写方案")
        self._wait(result["task_id"])
        with self.runner.lock:
            before_deliver = len([c for c in self.runner.calls if ":deliver:" in c["key"]])
        self.manager.retry("s1", result["task_id"], T.STAGE_REVIEW, "emp_ds4")
        deadline = time.time() + 6
        while time.time() < deadline:
            with self.runner.lock:
                reviews = len([c for c in self.runner.calls
                               if ":review:emp_ds4" in c["key"]])
            if reviews >= 2:
                break
            time.sleep(0.02)
        with self.runner.lock:
            after_deliver = len([c for c in self.runner.calls if ":deliver:" in c["key"]])
        self.assertEqual(after_deliver, before_deliver)  # 不自动重做交付
        task = self._task(result["task_id"])
        self.assertEqual(task["status"], T.STATUS_DELIVERED)
        self.manager.wait_idle()

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


class TestHttpApi(unittest.TestCase):
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

        cancelled = self.client.post(
            f"/api/sessions/{sid}/tasks/{tid}/cancel", json={"stage": "exec"}
        )
        self.assertEqual(cancelled.status_code, 200)
        retried = self.client.post(
            f"/api/sessions/{sid}/tasks/{tid}/retry", json={"stage": "exec"}
        )
        self.assertEqual(retried.status_code, 200)
        self.assertTrue(retried.get_json()["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
