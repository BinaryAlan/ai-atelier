"""删除单条消息的接口测试。

要锁住的行为：

1. 删除是「从会话里彻底移除」，不是打标记——上下文（build_context）直接读
   session["messages"]，物理删掉之后，后续任务的 Prompt 才真的不会带上它；
2. 只删指定的那一条，其他消息（包括 in_chat=false 的环节记录）原样保留；
3. 不存在的消息 / 非法会话 id 要有明确的 404 / 400，不能静默成功；
4. 删除只影响「以后拼的 Prompt」：已经跑完的任务里那条 `sent_prompt` 是发送当时的
   快照，不会因为消息被删就改写——否则等于篡改执行记录，事后没法复盘当时到底喂了什么。
"""

import unittest

import app as flask_app
import core.storage as storage
from core.tasks import build_context


class TestDeleteMessageApi(unittest.TestCase):
    def setUp(self):
        self.client = flask_app.app.test_client()
        session = storage.create_session(title="删除消息测试")
        self.sid = session["id"]
        session["messages"] = [
            {"id": "m1", "sender": "user", "sender_name": "我", "content": "第一句要删掉", "in_chat": True},
            {"id": "m2", "sender": "emp_agy", "sender_name": "agy", "content": "回答保留", "in_chat": True},
            {"id": "m3", "sender": "emp_agy", "sender_name": "agy", "content": "环节记录保留", "in_chat": False},
        ]
        storage.save_session(session)

    def tearDown(self):
        storage.delete_session(self.sid)

    def _remaining_ids(self):
        return [m["id"] for m in storage.get_session(self.sid)["messages"]]

    def test_delete_removes_only_target_message(self):
        r = self.client.delete(f"/api/sessions/{self.sid}/messages/m1")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["success"])
        self.assertEqual(self._remaining_ids(), ["m2", "m3"])

    def test_deleted_message_no_longer_enters_context(self):
        self.client.delete(f"/api/sessions/{self.sid}/messages/m1")
        session = storage.get_session(self.sid)
        context = build_context(session["messages"])
        joined = " ".join(item["content"] for item in context["messages"])
        self.assertNotIn("第一句要删掉", joined)
        self.assertIn("回答保留", joined)

    def test_completed_task_prompt_snapshot_keeps_deleted_text(self):
        """历史任务的 Prompt 是快照：消息删了，它里面仍能看到那段。"""
        session = storage.get_session(self.sid)
        session["tasks"] = {
            "t1": {
                "id": "t1",
                "type": "single",
                "status": "delivered",
                "part_a": {
                    "status": "done",
                    "sent_prompt": "【会话背景（最近 1 条）】\n我：第一句要删掉\n【当前任务】…",
                },
            }
        }
        storage.save_session(session)

        self.client.delete(f"/api/sessions/{self.sid}/messages/m1")
        session = storage.get_session(self.sid)

        # 聊天里没了、以后拼的 Prompt 也没了
        self.assertEqual([m["id"] for m in session["messages"]], ["m2", "m3"])
        joined = " ".join(i["content"] for i in build_context(session["messages"])["messages"])
        self.assertNotIn("第一句要删掉", joined)
        # 但那次执行实际发出去的 Prompt 保持原样（审计用，可复盘）
        self.assertIn("第一句要删掉", session["tasks"]["t1"]["part_a"]["sent_prompt"])

    def test_unknown_message_returns_404(self):
        r = self.client.delete(f"/api/sessions/{self.sid}/messages/not-there")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self._remaining_ids(), ["m1", "m2", "m3"])

    def test_invalid_session_id_rejected(self):
        r = self.client.delete("/api/sessions/bad%20id/messages/m1")
        self.assertEqual(r.status_code, 400)

    def test_deleting_twice_returns_404(self):
        self.assertEqual(self.client.delete(f"/api/sessions/{self.sid}/messages/m1").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/sessions/{self.sid}/messages/m1").status_code, 404)


if __name__ == "__main__":
    unittest.main()
