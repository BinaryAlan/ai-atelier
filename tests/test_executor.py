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


class TestModelScanning(unittest.TestCase):
    """本地模型扫描：按注册表发现已装 CLI、模型聚合、推理档位、旧缓存过期。"""

    AGY_OUT = (
        "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
        "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"
        "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
    )
    OPENCODE_OUT = (
        "github-copilot/claude-opus-5.5\n"
        "gitlab/duo-chat-gpt-5-4\n"
        "opencode/glm-5.3-flash-free\n"
        "opencode-go/beam-free\n"
        "garbage line\n"
    )
    CODEBUDDY_HELP = (
        "--model <model>  Model for the current session. "
        "Currently supported: (hy4free, hy4std)\n"
    )

    def _fake_run(self, argv, timeout=None):
        if argv[:2] == ["agy", "models"]:
            return self.AGY_OUT
        if argv[:2] == ["opencode", "models"]:
            return self.OPENCODE_OUT
        if argv[:2] == ["codebuddy", "--help"]:
            return self.CODEBUDDY_HELP
        return ""

    def test_agy_variants_are_merged_and_have_reasoning_levels(self):
        from core import scanner
        from core.cli_registry import SPEC_BY_NAME
        with mock.patch.object(scanner, "_run", return_value=self.AGY_OUT):
            models = scanner._scan_one(SPEC_BY_NAME["agy"])
        self.assertEqual([m["model_id"] for m in models],
                         ["gemini-3.8-flash", "claude-sonnet-4-6"])
        gem = models[0]
        self.assertEqual(gem["display_name"], "Gemini 3.8 Flash")
        self.assertEqual([l["id"] for l in gem["reasoning_levels"]], ["low", "high"])
        self.assertEqual(gem["default_reasoning"], "high")
        self.assertEqual(gem["cli_cmd"], 'agy --model gemini-3.8-flash-high -p "{prompt}"')
        self.assertNotIn("default", [m["model_id"] for m in models])

    def test_agy_falls_back_when_models_command_fails(self):
        from core import scanner
        from core.cli_registry import SPEC_BY_NAME
        with mock.patch.object(scanner, "_run", return_value=""):
            models = scanner._scan_one(SPEC_BY_NAME["agy"])
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["key"], "agy:default")

    def test_opencode_only_keeps_own_providers(self):
        from core import scanner
        from core.cli_registry import SPEC_BY_NAME
        with mock.patch.object(scanner, "_run", return_value=self.OPENCODE_OUT):
            models = scanner._scan_one(SPEC_BY_NAME["opencode"])
        ids = [m["model_id"] for m in models]
        self.assertEqual(ids, ["opencode/glm-5.3-flash-free", "opencode-go/beam-free"])
        self.assertFalse(any("github" in x or "gitlab" in x for x in ids))
        self.assertTrue(models[0]["is_free"])
        levels = models[0]["reasoning_levels"]
        self.assertEqual([l["id"] for l in levels], ["low", "medium", "high"])
        self.assertIn("#high", levels[-1]["cli_cmd"])

    def test_scan_discovers_every_installed_cli(self):
        import os
        import tempfile
        from core import scanner
        installed = {"agy", "codebuddy", "opencode", "claude", "codex", "cursor-agent"}
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "scanned_models.json")
            with mock.patch.object(scanner, "CACHE_FILE", cache), \
                 mock.patch.object(scanner.shutil, "which",
                                   side_effect=lambda n: "/usr/bin/fake" if n in installed else None), \
                 mock.patch.object(scanner, "_run", side_effect=self._fake_run):
                models = scanner.scan_local_models(force_refresh=True)
        by_cli = {}
        for m in models:
            by_cli[m["cli"]] = by_cli.get(m["cli"], 0) + 1
        self.assertEqual(set(by_cli), installed)
        self.assertTrue(any(m["is_free"] for m in models if m["model_id"] == "hy4free"))
        self.assertFalse(any(m["is_free"] for m in models if m["model_id"] == "hy4std"))

    def test_reasoning_modes_build_valid_commands(self):
        from core.cli_registry import CLI_SPECS, build_cli_cmd
        from core.executor import validate_cli_template
        for spec in CLI_SPECS:
            cmd = build_cli_cmd(spec["name"], "sample-model", "high")
            ok, err = validate_cli_template(cmd)
            self.assertTrue(ok, f"{spec['name']}: {err}")
        self.assertIn("--effort high", build_cli_cmd("codebuddy", "hy4std", "high"))
        self.assertIn("model_reasoning_effort=low", build_cli_cmd("codex", "gpt-5", "low"))
        self.assertIn("#high", build_cli_cmd("opencode", "opencode/beam-free", "high"))
        self.assertIn("--model sonnet", build_cli_cmd("claude", "sonnet", "default"))

    def test_session_reasoning_override_replaces_cli_cmd(self):
        from core.tasks import TaskManager
        high = 'agy --model gemini-3.8-flash-high -p "{prompt}"'
        low = 'agy --model gemini-3.8-flash-low -p "{prompt}"'
        settings = {"employees": [{"id": "emp_agy", "name": "agy", "cli_cmd": high}]}
        session = {"member_models": {"emp_agy": {
            "model_key": "agy:gemini-3.8-flash", "reasoning": "low", "cli_cmd": low}}}
        out = TaskManager._settings_for_session(None, session, settings)
        self.assertEqual(out["employees"][0]["cli_cmd"], low)
        self.assertEqual(out["employees"][0]["reasoning"], "low")
        self.assertEqual(settings["employees"][0]["cli_cmd"], high)  # 全局配置不被改脏

        bad = {"member_models": {"emp_agy": {"cli_cmd": "bash -c 'rm -rf /'"}}}
        out2 = TaskManager._settings_for_session(None, bad, settings)
        self.assertEqual(out2["employees"][0]["cli_cmd"], high)

    def test_member_models_api_rejects_unsafe_commands(self):
        from app import _clean_member_models
        ok, err = _clean_member_models({"emp_agy": {
            "model_key": "agy:gemini-3.8-flash", "reasoning": "high",
            "cli_cmd": 'agy --model gemini-3.8-flash-high -p "{prompt}"'}})
        self.assertIsNone(err)
        self.assertEqual(ok["emp_agy"]["reasoning"], "high")
        bad, err = _clean_member_models({"emp_agy": {"cli_cmd": "bash -c 'rm -rf /'"}})
        self.assertIsNotNone(err)
        self.assertIsNone(bad)

    def test_shipped_roles_use_high_reasoning(self):
        import json
        import os
        from core import scanner
        from core.executor import validate_cli_template
        path = os.path.join(scanner.BASE_DIR, "data", "settings.json")
        with open(path, encoding="utf-8") as f:
            emps = json.load(f)["employees"]
        self.assertTrue(emps)
        for e in emps:
            self.assertTrue(validate_cli_template(e["cli_cmd"])[0], e["id"])
            self.assertEqual(e.get("reasoning"), "high", e["id"])
        agy = next(e for e in emps if e["id"] == "emp_agy")
        self.assertIn("gemini-3.8-flash-high", agy["cli_cmd"])

    def test_legacy_list_cache_expires_by_mtime(self):
        import json
        import os
        import tempfile
        import time
        from core import scanner

        legacy = [{
            "key": "codebuddy:hy4free",
            "cli": "codebuddy",
            "model_id": "hy4free",
            "display_name": "hy4free",
            "cli_cmd": 'codebuddy --model hy4free --tools "" -p "{prompt}"',
        }]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scanned_models.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(legacy, f)
            old = time.time() - 2 * 24 * 3600
            os.utime(path, (old, old))
            with mock.patch.object(scanner, "CACHE_FILE", path):
                self.assertEqual(scanner._load_cache(), [])
                os.utime(path, None)  # 恢复成「刚刚写入」
                healed = scanner._load_cache()
        self.assertEqual(len(healed), 1)
        self.assertTrue(healed[0]["is_free"])  # 老缓存也要补上免费标记

    def test_opencode_template_allowed_and_plain_text(self):
        from core.executor import validate_cli_template, stream_flags, apply_access_mode
        tpl = 'opencode run --model opencode/beam-free "{prompt}"'
        ok, _ = validate_cli_template(tpl)
        self.assertTrue(ok)
        args = ["opencode", "run", "--model", "opencode/beam-free", "hi"]
        self.assertEqual(stream_flags(args), [])  # 不能塞 agy/codebuddy 的 stream-json
        self.assertIn("--auto", apply_access_mode(args, True, True))
        self.assertNotIn("--auto", apply_access_mode(args, False, True))


if __name__ == "__main__":
    unittest.main()
