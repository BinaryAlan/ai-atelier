"""文件预览接口的测试：预览弹窗依赖这两个接口。

要锁住的行为：

1. 文本读取把「太大」说清楚（`too_large` + `error`），前端才能给提示而不是
   把一句「文件过大」当正文渲染；
2. `/api/workspace/raw` 只放行图片类型，不能变成任意文件下载器；
3. 路径校验依旧生效（家目录 / 当前目录之外一律拒绝）。
"""

import os
import shutil
import tempfile
import unittest

import app as flask_app


class TestWorkspacePreviewApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # is_path_safe 只放行 home 与 cwd 下：临时目录放在 cwd 里才能通过校验
        cls.tmpdir = tempfile.mkdtemp(prefix="preview-", dir=os.getcwd())
        cls.client = flask_app.app.test_client()

        cls.md_path = os.path.join(cls.tmpdir, "note.md")
        with open(cls.md_path, "w", encoding="utf-8") as f:
            f.write("# 标题\n\n第一段\n")

        cls.png_path = os.path.join(cls.tmpdir, "pic.png")
        with open(cls.png_path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

        cls.big_path = os.path.join(cls.tmpdir, "big.txt")
        with open(cls.big_path, "w", encoding="utf-8") as f:
            f.write("x" * (600 * 1024))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_text_file_returns_content(self):
        r = self.client.get("/api/workspace/file", query_string={"path": self.md_path})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("# 标题", data["content"])
        self.assertEqual(data["name"], "note.md")
        self.assertNotIn("too_large", data)

    def test_large_file_reports_reason_instead_of_fake_content(self):
        r = self.client.get("/api/workspace/file", query_string={"path": self.big_path})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data.get("too_large"))
        self.assertEqual(data.get("content"), "")
        self.assertTrue(data.get("error"))

    def test_raw_serves_image(self):
        r = self.client.get("/api/workspace/raw", query_string={"path": self.png_path})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/png")
        self.assertTrue(r.data.startswith(b"\x89PNG"))
        r.close()  # send_file 会持有句柄，测试里显式关掉

    def test_raw_rejects_non_image(self):
        r = self.client.get("/api/workspace/raw", query_string={"path": self.md_path})
        self.assertEqual(r.status_code, 415)

    def test_raw_rejects_path_outside_workspace(self):
        r = self.client.get("/api/workspace/raw", query_string={"path": "/etc/hosts"})
        self.assertGreaterEqual(r.status_code, 400)

    def test_missing_file_is_404(self):
        r = self.client.get(
            "/api/workspace/file", query_string={"path": os.path.join(self.tmpdir, "nope.md")}
        )
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
