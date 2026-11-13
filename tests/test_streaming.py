"""流式输出的测试：暂存区、stream-json 解析、执行器注入、任务系统的接线。

三件事要锁住：

1. **暂存区是进程内、按会话隔离、会过期**：界面拿它做「正在写什么」，
   不能让已删会话 / 已结束的执行继续留在里面。
2. **解析器对 codebuddy 的 stream-json 事件足够宽容**：增量文本、思考、工具活动、
   最终 result 各取所需；事件被 read1 切成半行也不能丢内容。
3. **只有 codebuddy 的 print 模式才注入流式参数**：agy 之类没有这个开关，
   乱加会直接把 CLI 打挂（原来就是「跑完才吐」，也没法流式）。
"""

import io
import json
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.streaming as streaming
from core.executor import Executor, StreamJsonParser, stream_flags, wants_stream_json
from tests.test_tasks import FakeRunner, FakeStore, make_session, make_settings
from core import tasks as T


def event(payload):
    return json.dumps(payload, ensure_ascii=False) + "\n"


def text_delta(text):
    return event({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "text_delta", "text": text},
    }})


def thinking_delta(text):
    return event({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "thinking_delta", "thinking": text},
    }})


def result_event(text):
    return event({"type": "result", "subtype": "success", "result": text})


def message_start():
    return event({"type": "stream_event", "event": {"type": "message_start", "message": {}}})


def agy_step(step_index, *, delta=None, step_type="agent_response", tool=None, params=None):
    """agy 的一条 step_update；step_index 变了就是新一轮回答。"""
    step = {"conversation_id": "c1", "step_index": step_index, "state": "DONE", "step_type": step_type}
    if delta is not None:
        step["text_delta"] = delta
    if tool:
        step["tool_name"] = tool
        step["tool_info"] = {"name": tool, "parameters": params or {}}
    return event({"event": "step_update", "step_update": step})


def agy_result(text):
    return event({"event": "result", "result": {"status": "SUCCESS", "response": text}})


class TestStreamRegistry(unittest.TestCase):
    def setUp(self):
        streaming.clear_all()

    def tearDown(self):
        streaming.clear_all()

    def test_publish_and_read_by_session(self):
        streaming.publish("s1", "t_1:exec:emp_a", "t_1", "exec", "emp_a", "半句话", "writing")
        streaming.publish("s2", "t_2:exec:emp_b", "t_2", "exec", "emp_b", "别的会话", "writing")
        live = streaming.for_session("s1")
        self.assertEqual(list(live.keys()), ["t_1:exec:emp_a"])
        self.assertEqual(live["t_1:exec:emp_a"]["text"], "半句话")
        self.assertEqual(live["t_1:exec:emp_a"]["phase"], "writing")
        self.assertEqual(live["t_1:exec:emp_a"]["chars"], 3)

    def test_clear_and_clear_session(self):
        streaming.publish("s1", "k1", "t_1", "exec", "emp_a", "x")
        streaming.publish("s1", "k2", "t_1", "review", "emp_b", "y")
        streaming.clear("k1")
        self.assertNotIn("k1", streaming.for_session("s1"))
        streaming.clear_session("s1")
        self.assertEqual(streaming.for_session("s1"), {})

    def test_stale_stream_disappears(self):
        streaming.publish("s1", "k1", "t_1", "exec", "emp_a", "x")
        with streaming._LOCK:
            streaming._STREAMS["k1"]["updated_at"] = time.time() - streaming._STALE_SECONDS - 5
        self.assertIsNone(streaming.get("k1"))
        self.assertEqual(streaming.for_session("s1"), {})

    def test_text_is_capped(self):
        big = "字" * (streaming.MAX_LIVE_CHARS + 100)
        streaming.publish("s1", "k1", "t_1", "exec", "emp_a", big)
        self.assertLessEqual(streaming.for_session("s1")["k1"]["chars"], streaming.MAX_LIVE_CHARS)


class TestStreamJsonParser(unittest.TestCase):
    def test_text_thinking_and_tool_activity(self):
        p = StreamJsonParser()
        p.feed(thinking_delta("先想一下"))
        p.feed(text_delta("这是"))
        p.feed(text_delta("正文"))
        self.assertEqual(p.live_text, "先想一下\n\n这是正文")
        self.assertEqual(p.text and "".join(p.text), "这是正文")
        self.assertEqual(p.phase, "writing")

        p.feed(event({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "docs/plan.md"}},
        ]}}))
        self.assertEqual(p.phase, "tool")
        self.assertIn("Read", p.activity)
        self.assertIn("docs/plan.md", p.activity)

    def test_final_text_prefers_result_event(self):
        p = StreamJsonParser()
        p.feed(text_delta("草稿"))
        p.feed(result_event("最终回答"))
        self.assertEqual(p.final_text, "最终回答")

    def test_final_text_falls_back_to_deltas(self):
        p = StreamJsonParser()
        p.feed(text_delta("只有"))
        p.feed(text_delta("增量"))
        self.assertEqual(p.final_text, "只有增量")

    def test_split_lines_are_reassembled(self):
        p = StreamJsonParser()
        raw = text_delta("被切开的事件")
        for i in range(0, len(raw), 7):
            p.feed(raw[i:i + 7])
        p.finish()
        self.assertEqual(p.final_text, "被切开的事件")
        self.assertTrue(p.saw_event)

    def test_non_json_output_is_not_treated_as_events(self):
        p = StreamJsonParser()
        p.feed("这是普通文本\n不是 JSON\n")
        self.assertFalse(p.saw_event)
        self.assertEqual(p.final_text, "")

    def test_agy_multi_step_keeps_only_last_answer(self):
        """回归：agy 说到一半去读写文件时会换 step 重新开口。

        旧实现把所有 step 的 text_delta 首尾相接，落库就是
        「写到一半的前缀 + 另一段半截 + 重写后的完整回答」，聊天窗口里同一句话重复三遍。
        """
        p = StreamJsonParser()
        p.feed(agy_step(1, delta="已按要求完成对 [亲子教育硬核手册_标杆场景_A01_一坐到书桌前就尿急喝水抠橡皮.md](file:///Users/lc/Documents/social%20skill/亲子教育硬核手册_标杆场景_A01_一"))
        p.feed(agy_step(2, step_type="tool", tool="write_file",
                        params={"Path": "/Users/lc/Documents/social skill/a.md"}))
        p.feed(agy_step(3, delta="已按照要求完成对 [亲子教育硬核手册_标杆场景_A01_一坐到书桌前就尿急喝水抠橡皮.md](file:///Users/lc/Documents/social%20skill/亲子教育硬核手册_标杆场景_"))
        p.feed(agy_step(4, step_type="tool", tool="read_file", params={"Path": "/tmp/a.md"}))
        p.feed(agy_step(5, delta="已按要求完成对 [A01 实操指南：开工防磨蹭清障法](file:///x.md) 的全面修订，具体改动如下：\n"))
        final = p.final_text
        self.assertEqual(final, "已按要求完成对 [A01 实操指南：开工防磨蹭清障法](file:///x.md) 的全面修订，具体改动如下：\n")
        # 界面实时预览也不能把三段拼在一起
        self.assertNotIn("亲子教育硬核手册_标杆场景_A01_一坐到书桌前就尿急喝水抠橡皮.md", p.live_text)
        self.assertTrue(p.live_text.startswith("已按要求完成对 [A01 实操指南"))

    def test_codebuddy_second_message_replaces_first_draft(self):
        """codebuddy 侧同理：工具调用后会发新的 message_start。"""
        p = StreamJsonParser()
        p.feed(message_start())
        p.feed(text_delta("我先读一下文件"))
        p.feed(event({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "docs/a.md"}},
        ]}}))
        p.feed(message_start())
        p.feed(text_delta("已改好，"))
        p.feed(text_delta("共三处。"))
        self.assertEqual(p.final_text, "已改好，共三处。")
        self.assertEqual(p.live_text, "已改好，共三处。")

    def test_agy_result_wins_over_steps(self):
        """result.response 是权威答复，优先于任何 step 草稿。"""
        p = StreamJsonParser()
        p.feed(agy_step(1, delta="半截话"))
        p.feed(agy_step(2, delta="另一段半截"))
        p.feed(agy_result("最终答复"))
        self.assertEqual(p.final_text, "最终答复")


class TestStreamInjection(unittest.TestCase):
    def test_per_cli_flags(self):
        # codebuddy：逐字增量需要 --include-partial-messages
        self.assertEqual(
            stream_flags(["codebuddy", "-p", "x"]),
            ["--output-format", "stream-json", "--include-partial-messages", "--verbose"],
        )
        # agy：没有 --include-partial-messages，多给会直接报错
        self.assertEqual(stream_flags(["agy", "-p", "x"]), ["--output-format", "stream-json"])
        self.assertTrue(wants_stream_json(["codebuddy", "--print", "x"]))
        self.assertTrue(wants_stream_json(["agy", "-p", "x"]))
        # 模板自己指定了输出格式：不要覆盖，可能就是要 json
        self.assertFalse(wants_stream_json(["codebuddy", "--output-format", "json", "-p", "x"]))
        self.assertFalse(wants_stream_json(["agy", "--output-format", "text", "-p", "x"]))
        # 没有 print 模式时 stream-json 不生效
        self.assertFalse(wants_stream_json(["codebuddy", "x"]))


    def test_agy_step_events(self):
        """agy 的形状完全不同：event=step_update / result.response。"""
        p = StreamJsonParser()
        p.feed(event({"event": "init", "conversation_id": "c1", "init": {"cwd": "/tmp"}}))
        p.feed(event({"event": "step_update", "step_update": {
            "step_index": 0, "state": "DONE", "step_type": "user_input"}}))
        p.feed(event({"event": "step_update", "step_update": {
            "step_index": 1, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "第一句。"}}))
        p.feed(event({"event": "step_update", "step_update": {
            "step_index": 1, "state": "ACTIVE", "step_type": "tool_call", "tool_name": "Read"}}))
        p.feed(event({"event": "step_update", "step_update": {
            "step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": "第二句。"}}))
        p.feed(event({"event": "result", "result": {"status": "SUCCESS", "response": "第一句。第二句。"}}))

        self.assertTrue(p.saw_event)
        self.assertEqual(p.final_text, "第一句。第二句。")
        self.assertIn("第一句。", p.live_text)
        # user_input 这种步骤不该被当成「正在用工具」
        self.assertNotEqual(p.activity, "user_input")

    def test_agy_tool_step_shows_tool_name_not_step_type(self):
        """agy 的 step_type 只有笼统的 "tool"，有用的是 tool_name 与参数。

        之前直接把 step_type 当活动文案，界面上就出现一个「● tool」，
        用户完全看不出它在读文件还是跑命令（刷新页面后尤其明显）。
        """
        p = StreamJsonParser()
        p.feed(event({"event": "step_update", "step_update": {
            "step_index": 2, "state": "ACTIVE", "step_type": "tool",
            "tool_name": "read_file",
            "tool_info": {"name": "read_file", "parameters": {"Path": "/tmp/note.md"}},
        }}))
        self.assertEqual(p.phase, "tool")
        self.assertEqual(p.activity, "read_file · /tmp/note.md")

    def test_agy_generic_tool_without_name_falls_back_cleanly(self):
        p = StreamJsonParser()
        p.feed(event({"event": "step_update", "step_update": {
            "step_index": 3, "state": "ACTIVE", "step_type": "tool",
        }}))
        self.assertEqual(p.phase, "tool")
        self.assertEqual(p.activity, "tool")   # 前端会把这种泛型词换成「正在调用工具」


class FakeStreamProc:
    """最小可用的假进程：stdout/stderr 是真流，wait 立刻返回。"""

    def __init__(self, out="", err="", rc=0):
        self.returncode = rc
        self.stdout = io.TextIOWrapper(io.BytesIO(out.encode("utf-8")), encoding="utf-8")
        self.stderr = io.TextIOWrapper(io.BytesIO(err.encode("utf-8")), encoding="utf-8")
        self.pid = 4321
        self.wb_interrupted = False

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        return self.stdout.read(), self.stderr.read()


class TestExecutorStreaming(unittest.TestCase):
    def test_injects_flags_and_streams_progress(self):
        payload = (
            thinking_delta("先读文件")
            + text_delta("第一段")
            + text_delta("，第二段")
            + result_event("第一段，第二段")
        )
        seen = []
        proc = FakeStreamProc(out=payload)

        with mock.patch("subprocess.Popen", return_value=proc) as popen:
            result = Executor._run_cli(
                'codebuddy --model hy4std --tools "Read,Glob,Grep" -p "{prompt}"',
                "写点东西",
                session_id="s1",
                workspace_dir=ROOT,
                on_chunk=lambda text, info: seen.append((text, info.get("phase"))),
            )

        argv = popen.call_args[0][0]
        self.assertIn("--output-format", argv)
        self.assertIn("stream-json", argv)
        self.assertIn("--include-partial-messages", argv)
        # 原始模板不能被改写（用户看到的设置项应当仍是干净的那条）
        self.assertNotIn("stream-json", 'codebuddy -p "{prompt}"')
        # 最终输出取自 result 事件；中途回调用的是增量文本
        self.assertEqual(result.output, "第一段，第二段")
        self.assertEqual(result.raw_output, "第一段，第二段")
        self.assertTrue(any("第一段" in text for text, _ in seen))
        self.assertEqual(seen[-1][1], "writing")

    def test_template_with_explicit_format_is_not_streamed(self):
        """模板自己指定 --output-format text 时：不注入、不解析，退回纯文本。"""
        proc = FakeStreamProc(out="普通输出")
        with mock.patch("subprocess.Popen", return_value=proc) as popen:
            result = Executor._run_cli(
                'agy --output-format text -p "{prompt}"', "写点东西", session_id="s1",
                workspace_dir=ROOT, on_chunk=lambda text, info: None,
            )
        argv = popen.call_args[0][0]
        self.assertEqual(argv.count("--output-format"), 1)
        self.assertEqual(result.output, "普通输出")

    def test_no_callback_uses_communicate(self):
        proc = FakeStreamProc(out="一次性输出")

        def _boom(*a, **kw):
            raise AssertionError("传了 on_chunk 时才该走流式读取")

        with mock.patch("subprocess.Popen", return_value=proc), \
                mock.patch.object(Executor, "_stream_process", side_effect=_boom):
            result = Executor._run_cli('agy -p "{prompt}"', "写点东西", session_id="s1",
                                       workspace_dir=ROOT)
        self.assertEqual(result.output, "一次性输出")


class StreamingRunner(FakeRunner):
    """会回调 on_chunk 的假执行器：用来验证任务系统确实把流接上了。"""

    def __init__(self):
        super().__init__()
        self.streaming_started = threading.Event()
        self.release = threading.Event()

    def __call__(self, employee, prompt, session_id, workspace_dir, on_chunk=None):
        if on_chunk:
            on_chunk("已经写了一部分", {"phase": "writing", "chars": 7, "activity": ""})
        self.streaming_started.set()
        self.release.wait(5)
        return super().__call__(employee, prompt, session_id, workspace_dir)


class TestTaskManagerStreaming(unittest.TestCase):
    def setUp(self):
        streaming.clear_all()
        self.store = FakeStore(make_session())
        self.runner = StreamingRunner()
        self.manager = T.TaskManager(store=self.store, runner=self.runner)
        self.settings = make_settings()
        T.CANCELLED_KEYS.clear()

    def tearDown(self):
        self.runner.release.set()
        self.manager.wait_idle(timeout=5)
        streaming.clear_all()
        T.CANCELLED_KEYS.clear()

    def test_live_text_is_visible_while_running_then_cleared(self):
        self.assertTrue(self.manager._streams, "执行器支持 on_chunk 时应自动开启流式")
        result = self.manager.submit_message("s1", "@agy 写一段东西", self.settings)
        self.assertTrue(self.runner.streaming_started.wait(3))

        live = streaming.for_session("s1")
        key = f"{result['task_id']}:exec:emp_agy"
        self.assertIn(key, live)
        self.assertEqual(live[key]["text"], "已经写了一部分")
        self.assertEqual(live[key]["phase"], "writing")

        self.runner.release.set()
        self.manager.wait_idle(timeout=5)
        self.assertEqual(streaming.for_session("s1"), {}, "跑完之后暂存必须清空，否则界面会一直显示旧文本")

    def test_runner_without_on_chunk_keeps_working(self):
        """老测试夹具（不接受 on_chunk）不能因为流式上线而挂掉。"""
        store = FakeStore(make_session())
        manager = T.TaskManager(store=store, runner=FakeRunner())
        self.assertFalse(manager._streams)
        manager.submit_message("s1", "@agy 写一段东西", self.settings)
        manager.wait_idle(timeout=5)


if __name__ == "__main__":
    unittest.main()

class FailingStreamingRunner(StreamingRunner):
    """吐了半截正文就失败：模拟执行中被中断（崩溃 / 超时 / 被取消）。"""

    def __call__(self, employee, prompt, session_id, workspace_dir, on_chunk=None):
        if on_chunk:
            on_chunk("第三章写到一半", {"phase": "writing", "chars": 7, "activity": ""})
        return SimpleNamespace(status="error", output="调用失败", raw_output="调用失败", elapsed=0)


class TestInterruptHandoff(unittest.TestCase):
    """中断时把半成品留下来：重试靠它告诉成员「你写到哪了」。"""

    def setUp(self):
        streaming.clear_all()
        self.store = FakeStore(make_session())
        self.runner = FailingStreamingRunner()
        self.runner.release.set()  # 不需要真挂在流式中途
        self.manager = T.TaskManager(store=self.store, runner=self.runner)
        self.settings = make_settings()
        T.CANCELLED_KEYS.clear()

    def tearDown(self):
        self.runner.release.set()
        self.manager.wait_idle(timeout=5)
        streaming.clear_all()
        T.CANCELLED_KEYS.clear()

    def test_partial_text_survives_failure(self):
        result = self.manager.submit_message("s1", "@agy 写第三章", self.settings)
        # wait_idle 只保证队列空了，终态与「输入输出」的落库还差一拍
        deadline = time.time() + 5
        task = {}
        while time.time() < deadline:
            task = self.store.get_session("s1")["tasks"][result["task_id"]]
            if task.get("status") in (T.STATUS_FAILED, T.STATUS_TIMEOUT, T.STATUS_CANCELLED):
                break
            time.sleep(0.02)
        self.manager.wait_idle(timeout=5)
        session = self.store.get_session("s1")
        task = session["tasks"][result["task_id"]]
        self.assertEqual(task["status"], T.STATUS_FAILED)
        self.assertEqual((task.get("interrupt") or {}).get("partial"), "第三章写到一半")
        # 聊天正文仍是失败说明，但「输入输出」里能看到中断前写到哪
        record = [m for m in session["messages"] if m.get("kind") == "task_record"][0]
        self.assertIn("第三章写到一半", record["raw_response"])
        self.assertIn("中断前已输出", record["raw_response"])
        # 暂存区照常清空，界面上不会挂着上一轮的「正在写」
        self.assertEqual(streaming.for_session("s1"), {})
