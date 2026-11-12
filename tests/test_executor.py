"""CLI 执行层的测试：环境隔离、端口冲突重试、进程登记与回收。

两件事要锁住：

1. **环境隔离**（主要问题）。宿主（WorkBuddy / IDE）会把自己本地服务的端口以
   `SERVER__PORT` 注入所有子进程，而 codebuddy 启动时「环境里有就直接用，不再挑空闲端口」，
   于是每个 CLI 都去 listen 宿主的端口，100% 失败。所以起进程时必须构造干净环境。
2. **端口冲突重试**（兜底）。CLI 自己挑端口时，「探测空闲」与「真正 listen」之间有窗口期，
   并发两个实例仍可能撞车；零产出时重试一次，已经干过活的不重试。
"""

import unittest
from unittest import mock

from core.executor import (
    Executor,
    apply_access_mode,
    build_cli_env,
    is_port_conflict,
    normalize_cli_tools,
)


class FakeProc:
    def __init__(self, rc, out="", err="", interrupted=False):
        self.returncode = rc
        self._out = out
        self._err = err
        self.wb_interrupted = interrupted
        self.pid = 4242

    def communicate(self, timeout=None):
        return self._out, self._err


def run_cli_with(procs):
    """按顺序返回一堆假进程，记录 Popen 被调了几次。"""
    made = []
    calls = []

    def factory(*args, **kwargs):
        calls.append(kwargs)
        proc = procs[len(made)]
        made.append(proc)
        return proc

    with mock.patch("subprocess.Popen", side_effect=factory), \
            mock.patch("core.executor.time.sleep"):
        result = Executor._run_cli(
            'agy -p "{prompt}"', "写一段话", session_id="s1", workspace_dir="/tmp"
        )
    run_cli_with.last_calls = calls
    return result, made


class TestCliEnv(unittest.TestCase):
    """宿主环境变量必须被挡在门外，否则 CLI 会去抢宿主端口 / 认错自己的身份。

    用白名单而不是黑名单，是因为宿主注入的变量太多且随版本变化：实测有 81 个
    CODEBUDDY_*/WORKBUDDY_*/CLIENT_INFO_*/CLAUDE_*/SERVER__*，按前缀删仍漏掉了
    CLIENT_INFO_* 与 CLAUDE_* 的组合，导致「有时正常、有时 Authentication required」。
    """

    def test_host_identity_vars_are_excluded(self):
        env = build_cli_env({
            "PATH": "/usr/bin",
            "HOME": "/Users/someone",
            # 端口：会导致 CLI 去 listen 宿主的端口
            "SERVER__PORT": "62587",
            "SERVER__HOST": "127.0.0.1",
            "editor_sdk_port": "39099",
            # 会话/网关身份：会让 CLI 以为自己是宿主的子进程
            "CODEBUDDY_SESSION_KIND": "daemon",
            "CODEBUDDY_SESSION_NAME": "sso",
            "CODEBUDDY_GATEWAY_AUTH": "1",
            "CODEBUDDY_SANDBOX_BROKER_IPC_ADDRESS": "/tmp/x.sock",
            "WORKBUDDY_PAC_RPC_SOCKET": "/tmp/pac.sock",
            "PENDING_TELEMETRY_DIR": "/tmp/t",
            # IDE 身份：凑齐后 CLI 判定「我在 IDE 里」，改走 IDE 凭据通道
            "CLIENT_INFO_IDE_TYPE": "WorkBuddy",
            "CLIENT_INFO_PLATFORM": "darwin",
            "CLIENT_INFO_MACHINE_ID": "ABC-123",
            "CLAUDE_SESSION_ID": "ac3dc8a5",
            "CLAUDE_PROJECT_DIR": "/Users/someone/proj",
            "TARGET_BUNDLE_ID": "com.tencent.workbuddy.mac",
        })
        for gone in ("SERVER__PORT", "SERVER__HOST", "editor_sdk_port",
                     "CODEBUDDY_SESSION_KIND", "CODEBUDDY_SESSION_NAME",
                     "CODEBUDDY_GATEWAY_AUTH", "CODEBUDDY_SANDBOX_BROKER_IPC_ADDRESS",
                     "WORKBUDDY_PAC_RPC_SOCKET", "PENDING_TELEMETRY_DIR",
                     "CLIENT_INFO_IDE_TYPE", "CLIENT_INFO_PLATFORM", "CLIENT_INFO_MACHINE_ID",
                     "CLAUDE_SESSION_ID", "CLAUDE_PROJECT_DIR", "TARGET_BUNDLE_ID"):
            self.assertNotIn(gone, env, gone)
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["HOME"], "/Users/someone")

    def test_unknown_vars_are_not_forwarded(self):
        # 白名单的意义：将来宿主新增任何变量，默认都不带过去
        env = build_cli_env({"PATH": "/usr/bin", "SOME_FUTURE_HOST_VAR": "x"})
        self.assertNotIn("SOME_FUTURE_HOST_VAR", env)

    def test_user_credentials_and_external_proxy_are_kept(self):
        # 用户自己配的凭据、服务地址、外部代理必须保留，否则 CLI 连不上
        env = build_cli_env({
            "CODEBUDDY_API_KEY": "sk-test",
            "CODEBUDDY_BASE_URL": "https://example.com",
            "CODEBUDDY_CONFIG_DIR": "/Users/someone/.codebuddy",
            "HTTPS_PROXY": "http://10.0.0.8:8080",
            "NO_PROXY": "localhost",
        })
        self.assertEqual(env["CODEBUDDY_API_KEY"], "sk-test")
        self.assertEqual(env["CODEBUDDY_BASE_URL"], "https://example.com")
        self.assertEqual(env["CODEBUDDY_CONFIG_DIR"], "/Users/someone/.codebuddy")
        self.assertEqual(env["HTTPS_PROXY"], "http://10.0.0.8:8080")

    def test_host_sandbox_proxy_is_stripped(self):
        # 宿主把自己的沙箱代理塞进 HTTP_PROXY，CLI 走它会时好时坏
        env = build_cli_env({
            "HTTP_PROXY": "http://127.0.0.1:62616",
            "HTTPS_PROXY": "http://127.0.0.1:62616",
            "http_proxy": "http://localhost:62616",
            "ALL_PROXY": "socks5://127.0.0.1:62616",
        })
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "ALL_PROXY"):
            self.assertNotIn(key, env, key)

    def test_loopback_proxy_can_be_kept_on_demand(self):
        # 确实需要本机代理出网时的逃生口
        with mock.patch.dict("os.environ", {"AIMEET_KEEP_LOOPBACK_PROXY": "1"}):
            env = build_cli_env({"HTTP_PROXY": "http://127.0.0.1:7890"})
        self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:7890")

    def test_defaults_term_and_does_not_mutate_input(self):
        base = {"PATH": "/usr/bin", "SERVER__PORT": "62587"}
        env = build_cli_env(base)
        self.assertTrue(env.get("TERM"))
        self.assertIn("SERVER__PORT", base, "不应改动调用方传入的字典")

    def test_spawn_passes_clean_env(self):
        # 真正起进程时必须把干净环境传过去，光有函数没用
        with mock.patch.dict("os.environ", {"SERVER__PORT": "62587", "PATH": "/usr/bin"}):
            run_cli_with([FakeProc(0, out="ok")])
        kwargs = run_cli_with.last_calls[0]
        self.assertIn("env", kwargs, "Popen 必须显式传 env")
        self.assertNotIn("SERVER__PORT", kwargs["env"])


class TestPortConflict(unittest.TestCase):
    def test_detects_conflict_text(self):
        self.assertTrue(is_port_conflict(
            "Error: listen EADDRINUSE: address already in use 127.0.0.1:62587"
        ))
        self.assertTrue(is_port_conflict("listen EADDRINUSE"))
        self.assertFalse(is_port_conflict("model overloaded, retry later"))
        self.assertFalse(is_port_conflict(""))

    def test_retries_once_on_conflict_without_output(self):
        # 第一次：启动阶段就撞端口、零产出 → 重试；第二次成功
        result, made = run_cli_with([
            FakeProc(1, out="", err="Error: listen EADDRINUSE: address already in use 127.0.0.1:62587"),
            FakeProc(0, out='{"summary": "写好了"}'),
        ])
        self.assertEqual(result.status, "success")
        self.assertEqual(len(made), 2, "端口冲突应重试一次")
        self.assertIn("写好了", result.output)

    def test_does_not_retry_when_cli_already_produced_output(self):
        # 有产出说明已经干过活，重跑会重复工作
        result, made = run_cli_with([
            FakeProc(1, out="已完成一半的文件", err="listen EADDRINUSE 127.0.0.1:62587"),
        ])
        self.assertEqual(result.status, "error")
        self.assertEqual(len(made), 1)
        self.assertIn("端口被占用", result.output)

    def test_reports_hint_after_retries_exhausted(self):
        err = "Error: listen EADDRINUSE: address already in use 127.0.0.1:60464"
        result, made = run_cli_with([FakeProc(1, out="", err=err), FakeProc(1, out="", err=err)])
        self.assertEqual(result.status, "error")
        self.assertEqual(len(made), 2)
        self.assertIn("已自动重试", result.output)
        self.assertIn("EADDRINUSE", result.output)

    def test_plain_failure_is_not_retried(self):
        result, made = run_cli_with([FakeProc(2, out="", err="boom")])
        self.assertEqual(result.status, "error")
        self.assertEqual(len(made), 1)
        self.assertNotIn("端口", result.output)


class TestAccessMode(unittest.TestCase):
    """读权限与写权限是两个独立开关。

    历史 bug：新建成员时只有「写权限」一个勾选框（默认不勾），模型扫描缓存里
    又是 `--tools ""`，于是成员连产物都读不到，只会回一句「我先读取一下」就结束。
    这里锁住：只读成员也必须有 Read/Glob/Grep；写权限才追加 Write/Edit/Bash。
    """

    def test_read_only_role_gets_read_tools(self):
        out = apply_access_mode(
            ["codebuddy", "--model", "x", "--tools", "", "-p", "{prompt}"],
            write_access=False, read_access=True,
        )
        self.assertEqual(out[out.index("--tools") + 1], "Read,Glob,Grep")
        self.assertNotIn("-y", out)

    def test_write_role_gets_full_toolset(self):
        out = apply_access_mode(
            ["codebuddy", "--model", "x", "--tools", "", "-p", "{prompt}"],
            write_access=True, read_access=True,
        )
        self.assertEqual(
            out[out.index("--tools") + 1], "Read,Glob,Grep,Write,Edit,Bash"
        )
        self.assertIn("-y", out)

    def test_both_off_disables_tools(self):
        out = apply_access_mode(
            ["codebuddy", "--tools", "Read,Glob,Grep", "-p", "{prompt}"],
            write_access=False, read_access=False,
        )
        self.assertEqual(out[out.index("--tools") + 1], "")

    def test_write_only_drops_read_tools(self):
        out = apply_access_mode(
            ["codebuddy", "--tools", "Read,Glob,Grep", "-p", "{prompt}"],
            write_access=True, read_access=False,
        )
        self.assertEqual(out[out.index("--tools") + 1], "Write,Edit,Bash")

    def test_agy_only_gains_flag_when_writable(self):
        self.assertEqual(
            apply_access_mode(["agy", "-p", "{prompt}"], False, True),
            ["agy", "-p", "{prompt}"],
        )
        self.assertIn(
            "--dangerously-skip-permissions",
            apply_access_mode(["agy", "-p", "{prompt}"], True, True),
        )

    def test_template_heals_empty_tools(self):
        """模板里遗留的 --tools "" 要按读权限就地修好，否则界面上看到的就是假的。"""
        cmd = normalize_cli_tools(
            'codebuddy --model hy4std --tools "" -p "{prompt}"', True, False
        )
        self.assertIn('--tools "Read,Glob,Grep"', cmd)

    def test_template_keeps_explicit_default(self):
        cmd = normalize_cli_tools('codebuddy --tools "default" -p "{p}"', False, False)
        self.assertIn('--tools "default"', cmd)


class TestScannedModelsCacheHeal(unittest.TestCase):
    """旧扫描缓存（纯列表格式、--tools ""）不能直接把死模板喂给新建成员。"""

    def test_legacy_cache_is_healed(self):
        import json
        import os
        import tempfile
        from unittest import mock
        from core import scanner

        legacy = [{
            "key": "codebuddy:hy4std",
            "cli": "codebuddy",
            "model_id": "hy4std",
            "display_name": "hy4std",
            "cli_cmd": 'codebuddy --model hy4std --tools "" -p "{prompt}"',
        }]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scanned_models.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(legacy, f)
            with mock.patch.object(scanner, "CACHE_FILE", path):
                models = scanner._load_cache()
        self.assertEqual(len(models), 1)
        self.assertIn('--tools "Read,Glob,Grep"', models[0]["cli_cmd"])


if __name__ == "__main__":
    unittest.main()
