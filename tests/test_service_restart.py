"""服务自我替换（重启）链路的回归测试。

背景：`./run.sh restart` 曾稳定卡满 30 秒并超时，之后端口 5050 再没人监听——
不是「重启慢」，而是新进程根本没起来。根因是 `_spawn_successor()` 让继任进程
**继承**父进程的 stdin/stdout/stderr：父进程一 `os._exit(0)`，那根管道或伪终端的
读端随之消失，继任进程写第一个 `print()`（启动横幅）就拿到 EPIPE/EIO，
Python 于是以退出码 120 退出（它仍会尝试 flush 一次 stdio，失败即 120）。

所以这里锁住两件事：
1. 继任进程的 stdio 一律重定向（stdin=DEVNULL、stdout/stderr 写日志文件）；
2. 日志文件超限会轮转，且 `/api/system/restart` 会把日志路径回给调用方。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import app as flask_app


class TestSuccessorSpawn(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="restart-")
        self.log_path = os.path.join(self.tmpdir, ".run", "service.log")
        self._patch = mock.patch.object(flask_app, "_SERVICE_LOG", self.log_path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _spawn(self):
        with mock.patch.object(sys, "argv", [os.path.abspath("app.py")]):
            return flask_app._spawn_successor()

    def test_child_does_not_inherit_stdio(self):
        with mock.patch("subprocess.Popen") as popen:
            self.assertTrue(self._spawn())
        kwargs = popen.call_args.kwargs
        # stdin 不能继承：否则子进程可能读走本该属于旧进程的输入
        self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)
        # stdout/stderr 必须是日志文件，不能是 None（None 表示继承父进程）
        self.assertIsNotNone(kwargs.get("stdout"))
        self.assertTrue(hasattr(kwargs.get("stdout"), "write"))
        self.assertEqual(kwargs.get("stderr"), subprocess.STDOUT)
        self.assertTrue(kwargs.get("start_new_session"))

    def test_spawn_writes_marker_into_log(self):
        with mock.patch("subprocess.Popen"):
            self.assertTrue(self._spawn())
        with open(self.log_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("拉起继任实例", content)

    def test_unknown_entry_is_left_to_the_user(self):
        # 不是 `python app.py` 启动的（gunicorn / flask run）不该假装能自重启
        with mock.patch.object(sys, "argv", ["/usr/local/bin/gunicorn"]):
            with mock.patch("subprocess.Popen") as popen:
                self.assertFalse(flask_app._spawn_successor())
        popen.assert_not_called()

    def test_oversized_log_is_rotated(self):
        os.makedirs(os.path.dirname(self.log_path))
        with open(self.log_path, "wb") as f:
            f.write(b"x" * (2 * 1024 * 1024 + 1))
        with mock.patch("subprocess.Popen"):
            self.assertTrue(self._spawn())
        self.assertTrue(os.path.exists(self.log_path + ".1"))
        self.assertLess(os.path.getsize(self.log_path), 2 * 1024 * 1024)


class TestInheritedStdioIsFatal(unittest.TestCase):
    """把「为什么必须重定向」用真实子进程固定下来。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="deadpipe-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_dead_pipe_kills_child_but_file_does_not(self):
        code = ["-c", "print('banner ok')"]

        # 1) 继承一根读端已关闭的管道：子进程写 stdout 就崩
        r, w = os.pipe()
        os.close(r)
        dead = subprocess.Popen([sys.executable] + code, stdout=w, stderr=w)
        os.close(w)
        self.assertNotEqual(dead.wait(), 0)

        # 2) 同样的子进程，stdout 指向文件：正常退出，内容落盘
        log = os.path.join(self.tmpdir, "ok.log")
        with open(log, "wb") as fh:
            alive = subprocess.Popen([sys.executable] + code, stdout=fh, stderr=fh)
        self.assertEqual(alive.wait(), 0)
        with open(log, encoding="utf-8") as fh:
            self.assertIn("banner ok", fh.read())


class TestRestartEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = flask_app.app.test_client()

    def test_requires_action_header(self):
        r = self.client.post("/api/system/restart")
        self.assertEqual(r.status_code, 400)

    def test_schedules_restart_and_reports_log_path(self):
        with mock.patch.object(flask_app, "_schedule_exit") as schedule:
            r = self.client.post("/api/system/restart",
                                 headers={"X-Atelier-Action": "1"})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(data["log_file"].endswith("service.log"))
        schedule.assert_called_once_with("restart")

    def test_legacy_meeting_header_also_accepted(self):
        with mock.patch.object(flask_app, "_schedule_exit") as schedule:
            r = self.client.post("/api/system/restart",
                                 headers={"X-Meeting-Action": "1"})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["success"])


if __name__ == "__main__":
    unittest.main()
