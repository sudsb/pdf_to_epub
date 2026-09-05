"""guimanage 后端与 CLI 接线测试（stdlib unittest，中文 docstring）。

覆盖：GET / 返回 UI、/api/config 读写、/api/status、/api/ping、/api/bye、
/api/server/start|stop、/api/pick（cancelled 路径，不真弹 tkinter）、
_browser_gone 判定，以及 mian.py 的 gui 子命令与终端菜单第 8 项接线。

测试期间 monkeypatch configmanage._CONFIG_PATH 到临时文件（严禁写真实 config.json）。
"""

import json
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

import configmanage
import guimanage
import mian


def _minimal_config() -> dict:
    """最小合法 config（写入临时 _CONFIG_PATH 用）。"""
    return {
        "llama_server": "E:/xox/Tools/llama-c/llama-server.exe",
        "models_dir": "E:/xox/Tools/llama-c/models",
        "engine": "llama",
        "selected_model": "HY",
        "model_choices": {
            "HY": {
                "name": "HunyuanOCR.BF16.gguf",
                "mmproj": "HunyuanOCR.mmproj-bf16.gguf",
            },
        },
        "llama_server_args": {"host": "127.0.0.1", "port": "8080"},
        "vllm_server_args": {"host": "127.0.0.1", "port": "8000"},
        "proofread": {"enable_llm": False, "llm_model": "qwen2b"},
        "shortcuts": {},
        "format_rules": [],
    }


class GuiServerTestBase(unittest.TestCase):
    """启动测试 HTTP 服务（port=0，open_browser=False，serve 在 daemon 线程）。"""

    def setUp(self):
        # 严禁写真实 config.json：monkeypatch _CONFIG_PATH 到临时文件
        fd, self._cfg_path = tempfile.mkstemp(prefix="test_gui_cfg_", suffix=".json")
        os.close(fd)
        with open(self._cfg_path, "w", encoding="utf-8") as f:
            json.dump(_minimal_config(), f, ensure_ascii=False)
        self._orig_cfg_path = configmanage._CONFIG_PATH
        configmanage._CONFIG_PATH = self._cfg_path

        self._state = {
            "finished": threading.Event(),
            "dlg_queue": queue.Queue(),
            "dlg_lock": threading.Lock(),
            "serve_lock": threading.Lock(),
            "gone_at": None,
            "last_beat": time.monotonic(),
            "beat_lock": threading.Lock(),
            "last_error": None,
            "convert": {
                "lock": threading.Lock(),
                "proc": None,
                "lines": [],
                "running": False,
                "done": False,
                "success": False,
                "exit_code": None,
                "epub_path": None,
                "error": None,
                "prompt": None,
            },
        }
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), guimanage._GuiHandler)
        self._server.daemon_threads = True
        self._server.state = self._state
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._port = self._server.server_address[1]
        self._base = f"http://127.0.0.1:{self._port}"

    def tearDown(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        configmanage._CONFIG_PATH = self._orig_cfg_path
        try:
            os.unlink(self._cfg_path)
        except OSError:
            pass

    def _get(self, path):
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(self._base + path, timeout=5) as resp:
                return resp.status, resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.headers.get("Content-Type", ""), e.read()
            finally:
                e.close()

    def _post(self, path, body=None):
        import urllib.error
        import urllib.request

        data = (
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None
            else b""
        )
        req = urllib.request.Request(
            self._base + path,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            finally:
                e.close()


class TestGuiEndpoints(GuiServerTestBase):
    """HTTP 端点测试。"""

    def test_get_root_serves_ui(self):
        """GET / 返回 200 text/html 且含真实 UI 标记。"""
        status, ctype, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("<!DOCTYPE html>", body.decode("utf-8"))
        self.assertIn("PToEA 配置界面", body.decode("utf-8"))

    def test_get_config_ok(self):
        """GET /api/config 返回 ok 且含 config/models/path。"""
        status, _, body = self._get("/api/config")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertIn("config", data)
        self.assertIn("models", data)
        self.assertIn("path", data)
        self.assertEqual(data["config"]["engine"], "llama")
        self.assertEqual(data["config"]["selected_model"], "HY")
        # models 列表含模型文件存在性字段；validate_and_patch_config 会把
        # DEFAULT_CONFIG 的 model_choices 合并进来，故只断言 HY 存在而非精确数量
        model_keys = [m["key"] for m in data["models"]]
        self.assertIn("HY", model_keys)
        hy = next(m for m in data["models"] if m["key"] == "HY")
        self.assertIn("name_exists", hy)
        self.assertIn("mmproj_exists", hy)

    def test_get_status_ok(self):
        """GET /api/status 返回 ok 且含 engine/probe（mock 探测避免真实网络）。"""
        import llamamanage

        with mock.patch.object(llamamanage, "_probe_server", return_value="none"):
            status, _, body = self._get("/api/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["engine"], "llama")
        self.assertEqual(data["probe"], "none")
        self.assertEqual(data["model_key"], "HY")
        self.assertEqual(data["port"], "8080")
        self.assertIn("busy", data)

    def test_post_config_invalid_engine_400(self):
        """POST /api/config 非法 engine → 400。"""
        status, body = self._post("/api/config", {"engine": "bad"})
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertIn("engine", data["error"])

    def test_post_config_unknown_model_400(self):
        """POST /api/config selected_model 不在 model_choices → 400。"""
        status, body = self._post(
            "/api/config",
            {"selected_model": "NOPE", "model_choices": {"HY": {"name": "x", "mmproj": "y"}}},
        )
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertIn("未知模型", data["error"])

    def test_post_config_valid_write(self):
        """POST /api/config 合法写入 → 200 且临时文件内容变化（原子写）。"""
        status, body = self._post("/api/config", {"engine": "vllm"})
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        # 原子写后重新读取校验
        with open(self._cfg_path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["engine"], "vllm")
        self.assertEqual(configmanage.get_config(show_dialogs=False)["engine"], "vllm")
    def test_post_config_case_insensitive(self):
        """POST /api/config accepts lowercase selected_model when model_choices has uppercase key."""
        body = {"selected_model": "ulq4", "model_choices": {"ULQ4": {"name": "x", "mmproj": "y"}}}
        status, resp = self._post("/api/config", body)
        self.assertEqual(status, 200)
        data = json.loads(resp)
        self.assertTrue(data["ok"])
        # on-disk selected_model should be canonicalized to ULQ4
        with open(self._cfg_path, 'r', encoding='utf-8') as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk.get('selected_model'), 'ULQ4')


    def test_post_server_stop_ok(self):
        """POST /api/server/stop → ok（mock llamamanage.stopserver）。"""
        import llamamanage

        with mock.patch.object(llamamanage, "stopserver") as stop:
            status, body = self._post("/api/server/stop")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        stop.assert_called_once()

    def test_post_pick_cancelled(self):
        """POST /api/pick cancelled 路径：monkeypatch _drain_dialog_queue 注入结果，不真弹 tkinter。"""
        def fake_drain(state):
            while True:
                try:
                    req = state["dlg_queue"].get_nowait()
                except queue.Empty:
                    time.sleep(0.01)
                    continue
                req["result"] = {"ok": True, "cancelled": True}
                req["done"].set()
                return

        orig = guimanage._drain_dialog_queue
        guimanage._drain_dialog_queue = fake_drain
        drainer = threading.Thread(
            target=lambda: guimanage._drain_dialog_queue(self._state), daemon=True
        )
        try:
            drainer.start()
            status, body = self._post("/api/pick", {"kind": "file"})
        finally:
            guimanage._drain_dialog_queue = orig
            drainer.join(timeout=5)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertTrue(data["cancelled"])

    def test_get_ping_ok(self):
        """GET /api/ping → ok 且刷新 last_beat。"""
        self._state["last_beat"] = 0.0
        status, _, body = self._get("/api/ping")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertGreater(self._state["last_beat"], 0.0)

    def test_post_bye_ok(self):
        """POST /api/bye → ok 且置 gone_at。"""
        self.assertIsNone(self._state["gone_at"])
        status, body = self._post("/api/bye")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertIsNotNone(self._state["gone_at"])

    def test_post_server_start_unknown_model_400(self):
        """POST /api/server/start 未知模型 → 400。"""
        status, body = self._post("/api/server/start", {"model": "NOPE"})
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertIn("未知模型", data["error"])

    def test_post_invalid_json_400(self):
        """POST 请求体解析失败 → 400 无效的 JSON。"""
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            self._base + "/api/config",
            data=b"{not json",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("应返回 400")
        except urllib.error.HTTPError as e:
            try:
                self.assertEqual(e.code, 400)
                data = json.loads(e.read())
            finally:
                e.close()
            self.assertFalse(data["ok"])
            self.assertIn("无效的 JSON", data["error"])

    def test_unknown_path_404(self):
        """未知路径 → 404。"""
        status, _, body = self._get("/api/nope")
        self.assertEqual(status, 404)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "未找到")


class TestBrowserGone(unittest.TestCase):
    """_browser_gone 判定逻辑。"""

    def test_browser_gone_heartbeat_alive(self):
        """心跳正常 → 未失联。"""
        now = time.monotonic()
        state = {"gone_at": None, "last_beat": now}
        gone, stale = guimanage._browser_gone(state, idle_timeout=120, now=now)
        self.assertFalse(gone)
        self.assertIsNone(stale)

    def test_browser_gone_heartbeat_stale(self):
        """心跳失联超过 idle_timeout*2 → 判定关闭（需连续确认）。"""
        now = time.monotonic()
        state = {"gone_at": None, "last_beat": now - 300}
        # 首次失联：返回 False，开始计时
        gone, stale = guimanage._browser_gone(state, idle_timeout=120, now=now)
        self.assertFalse(gone)
        self.assertEqual(stale, now)
        # 超过 _STALE_CONFIRM_SECONDS 后判定
        gone, _ = guimanage._browser_gone(
            state, idle_timeout=120, now=now + 4, stale_since=stale
        )
        self.assertTrue(gone)

    def test_browser_gone_gone_at_timeout(self):
        """gone_at 信标：超过 idle_timeout 判定，未超过不判定。"""
        now = time.monotonic()
        state = {"gone_at": now - 121, "last_beat": now}
        gone, _ = guimanage._browser_gone(state, idle_timeout=120, now=now)
        self.assertTrue(gone)
        state = {"gone_at": now - 10, "last_beat": now}
        gone, _ = guimanage._browser_gone(state, idle_timeout=120, now=now)
        self.assertFalse(gone)


class TestBrowserGoneGuard(GuiServerTestBase):
    """浏览器关闭监测 guard：刷新场景 + 信标倒计时 + 心跳失联连续确认。

    覆盖报告的 bug：/api/ping 不清空 gone_at 导致页面刷新后仍被判定关闭。
    """

    def test_refresh_scenario_ping_clears_gone_at(self):
        """REFRESH SCENARIO（报告的 bug）：/api/bye 置 gone_at，随后 /api/ping
        应清空 gone_at；即使 idle_timeout 已过，_browser_gone 仍返回 False。"""
        # 1. 模拟 pagehide 信标：POST /api/bye
        status, body = self._post("/api/bye")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertIsNotNone(self._state["gone_at"])
        gone_at_time = self._state["gone_at"]

        # 2. 页面刷新/恢复：GET /api/ping 应清空 gone_at
        status, _, body = self._get("/api/ping")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertIsNone(self._state["gone_at"], "心跳应取消关闭倒计时")

        # 3. 即使 now 超过 gone_at + idle_timeout，gone_at 已清空 → 不判定关闭
        #    （过去若 /api/ping 不清 gone_at，此处 now - gone_at >= idle_timeout 判定 True）
        now = gone_at_time + 121  # 超过 idle_timeout(120)
        gone, _ = guimanage._browser_gone(
            self._state, idle_timeout=120, now=now, stale_since=None
        )
        self.assertFalse(gone, "刷新后 gone_at 已清空，应不判定关闭")

    def test_gone_at_countdown_triggers(self):
        """gone_at 信标倒计时：now - gone_at >= idle_timeout → True。"""
        now = time.monotonic()
        state = {"gone_at": now - 120, "last_beat": now}
        gone, _ = guimanage._browser_gone(state, idle_timeout=120, now=now)
        self.assertTrue(gone)

    def test_heartbeat_loss_needs_continuous_confirmation(self):
        """心跳失联需连续确认：首次失联记时不判定，超过 _STALE_CONFIRM_SECONDS 才判定。"""
        now = time.monotonic()
        state = {"gone_at": None, "last_beat": now - 9999}
        # 首次失联：返回 False，记下 stale_since
        gone, stale = guimanage._browser_gone(state, idle_timeout=120, now=now)
        self.assertFalse(gone)
        self.assertEqual(stale, now)
        # 未满 _STALE_CONFIRM_SECONDS：仍不判定
        gone, _ = guimanage._browser_gone(
            state,
            idle_timeout=120,
            now=now + guimanage._STALE_CONFIRM_SECONDS - 0.1,
            stale_since=stale,
        )
        self.assertFalse(gone)
        # 超过 _STALE_CONFIRM_SECONDS：判定
        gone, _ = guimanage._browser_gone(
            state,
            idle_timeout=120,
            now=now + guimanage._STALE_CONFIRM_SECONDS + 0.1,
            stale_since=stale,
        )
        self.assertTrue(gone)

    def test_healthy_heartbeat_resets_stale(self):
        """心跳恢复：返回 (False, None)，stale_since 重置。"""
        now = time.monotonic()
        state = {"gone_at": None, "last_beat": now}
        gone, stale = guimanage._browser_gone(
            state, idle_timeout=120, now=now, stale_since=now - 100
        )
        self.assertFalse(gone)
        self.assertIsNone(stale)


class TestOpenDisplay(unittest.TestCase):
    """_open_display 助手函数测试（monkeypatch configmanage.get_config + webview/webbrowser）。"""

    def setUp(self):
        self._orig_get_config = configmanage.get_config
        self._orig_modules = dict(sys.modules)

    def tearDown(self):
        configmanage.get_config = self._orig_get_config
        sys.modules.clear()
        sys.modules.update(self._orig_modules)

    def test_open_display_browser_mode(self):
        """gui_display='browser' → 调用 _open_browser，返回 ("browser", None)。"""
        calls = []

        def fake_get_config(show_dialogs=True):
            return {"gui_display": "browser", "browser": ""}

        configmanage.get_config = fake_get_config
        guimanage._open_browser = lambda url: calls.append(url)

        role, win = guimanage._open_display("http://test/", "Test Title")
        self.assertEqual(role, "browser")
        self.assertIsNone(win)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], "http://test/")

    def test_open_display_pywebview_success(self):
        """gui_display='pywebview' + webview 可用 → 返回 ("owner", window)，不调用 _open_browser。"""
        calls = []
        sentinel = object()

        def fake_get_config(show_dialogs=True):
            return {"gui_display": "pywebview", "browser": "", "window_maximized": True}

        fake_webview = mock.MagicMock()
        fake_webview.create_window.return_value = sentinel
        sys.modules["webview"] = fake_webview

        configmanage.get_config = fake_get_config
        guimanage._open_browser = lambda url: calls.append(url)

        role, win = guimanage._open_display("http://test", "Test Title")
        self.assertEqual(role, "owner")
        self.assertIs(win, sentinel)
        fake_webview.create_window.assert_called_once_with("Test Title", "http://test/tabhost", maximized=True)
        self.assertEqual(len(calls), 0)

    def test_open_display_pywebview_create_raises(self):
        """webview.create_window 抛异常 → 回退 _open_browser，返回 ("browser", None)。"""
        calls = []

        def fake_get_config(show_dialogs=True):
            return {"gui_display": "pywebview", "browser": "", "window_maximized": True}

        fake_webview = mock.MagicMock()
        fake_webview.create_window.side_effect = RuntimeError("init failed")
        sys.modules["webview"] = fake_webview

        configmanage.get_config = fake_get_config
        guimanage._open_browser = lambda url: calls.append(url)

        role, win = guimanage._open_display("http://test/", "Test Title")
        self.assertEqual(role, "browser")
        self.assertIsNone(win)
        self.assertEqual(len(calls), 1)

    def test_open_display_pywebview_import_fails(self):
        """import webview 失败 → 回退 _open_browser，返回 ("browser", None)。"""
        calls = []

        def fake_get_config(show_dialogs=True):
            return {"gui_display": "pywebview", "browser": "", "window_maximized": True}

        # 设置 webview 模块为 None 导致 ImportError
        sys.modules["webview"] = None

        configmanage.get_config = fake_get_config
        guimanage._open_browser = lambda url: calls.append(url)

        role, win = guimanage._open_display("http://test/", "Test Title")
        self.assertEqual(role, "browser")
        self.assertIsNone(win)
        self.assertEqual(len(calls), 1)


class TestGuiConfigPost(unittest.TestCase):
    """POST /api/config 新增 gui_display 校验测试。"""

    def setUp(self):
        fd, self._cfg_path = tempfile.mkstemp(prefix="test_gui_cfg_", suffix=".json")
        os.close(fd)
        with open(self._cfg_path, "w", encoding="utf-8") as f:
            json.dump(_minimal_config(), f, ensure_ascii=False)
        self._orig_cfg_path = configmanage._CONFIG_PATH
        configmanage._CONFIG_PATH = self._cfg_path

        self._state = {
            "finished": threading.Event(),
            "dlg_queue": queue.Queue(),
            "dlg_lock": threading.Lock(),
            "serve_lock": threading.Lock(),
            "gone_at": None,
            "last_beat": time.monotonic(),
            "beat_lock": threading.Lock(),
            "last_error": None,
            "convert": {"lock": threading.Lock(), "proc": None, "lines": [], "running": False, "done": False, "success": False, "exit_code": None, "epub_path": None, "error": None, "prompt": None},
            "correct": {"lock": threading.Lock(), "proc": None, "lines": [], "running": False, "done": False, "success": False, "exit_code": None, "error": None, "prompt": None},
            "merge": {"lock": threading.Lock(), "lines": [], "running": False, "done": False, "success": False, "error": None, "out_path": None, "stop_event": threading.Event()},
        }
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), guimanage._GuiHandler)
        self._server.daemon_threads = True
        self._server.state = self._state
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._port = self._server.server_address[1]
        self._base = f"http://127.0.0.1:{self._port}"

    def tearDown(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        configmanage._CONFIG_PATH = self._orig_cfg_path
        try:
            os.unlink(self._cfg_path)
        except OSError:
            pass

    def _post(self, path, body):
        import urllib.request
        import urllib.error

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self._base + path, data=data, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def test_post_config_gui_display_invalid(self):
        """POST /api/config gui_display='evil' → 400 中文错误。"""
        status, body = self._post("/api/config", {"gui_display": "evil"})
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertIn("gui_display 仅支持 pywebview / browser", data["error"])

    def test_post_config_gui_display_browser(self):
        """POST /api/config gui_display='browser' → 200。"""
        status, body = self._post("/api/config", {"gui_display": "browser"})
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        # 磁盘持久化验证
        with open(self._cfg_path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["gui_display"], "browser")


class TestTabEndpoints(GuiServerTestBase):
    """标签页会话与 /api/tabs 端点测试。"""

    def setUp(self):
        super().setUp()
        # monkeypatch tabmanage.TAB_SESSION_PATH 到临时文件
        import tabmanage

        self._orig_session_path = tabmanage.TAB_SESSION_PATH
        fd, self._tmp_session = tempfile.mkstemp(prefix="test_tab_session_", suffix=".json")
        os.close(fd)
        tabmanage.TAB_SESSION_PATH = self._tmp_session
        # 也要 patch 模块内的 _tab_session_path() 返回值
        self._orig_tab_session_path = tabmanage._tab_session_path
        tabmanage._tab_session_path = lambda: self._tmp_session

    def tearDown(self):
        import tabmanage

        tabmanage.TAB_SESSION_PATH = self._orig_session_path
        tabmanage._tab_session_path = self._orig_tab_session_path
        try:
            os.unlink(self._tmp_session)
        except OSError:
            pass
        super().tearDown()

    def test_register_tab_first_is_owner(self):
        """首次 register_tab → owner，会话文件创建。"""
        import tabmanage

        with mock.patch.object(tabmanage, "_owner_alive", return_value=False):
            role = tabmanage.register_tab("配置中心", "http://127.0.0.1:55044/", "http://127.0.0.1:55044/")
        self.assertEqual(role, "owner")
        self.assertTrue(os.path.exists(self._tmp_session))
        with open(self._tmp_session, "r", encoding="utf-8") as f:
            session = json.load(f)
        self.assertEqual(session["owner_pid"], os.getpid())
        self.assertEqual(session["owner_base"], "http://127.0.0.1:55044/")
        self.assertEqual(len(session["tabs"]), 1)
        self.assertEqual(session["tabs"][0]["title"], "配置中心")

    def test_register_tab_second_is_guest(self):
        """owner 存活时第二次 register_tab → guest，tabs 列表有 2 项。"""
        import tabmanage

        with mock.patch.object(tabmanage, "_owner_alive", return_value=True):
            # 先注册 owner
            role1 = tabmanage.register_tab("配置中心", "http://127.0.0.1:55044/", "http://127.0.0.1:55044/")
            self.assertEqual(role1, "owner")
            # 再注册 guest（相同 owner_base，owner 存活）
            role2 = tabmanage.register_tab("文字矫正", "http://127.0.0.1:55045/", "http://127.0.0.1:55045/")
            self.assertEqual(role2, "guest")
        with open(self._tmp_session, "r", encoding="utf-8") as f:
            session = json.load(f)
        self.assertEqual(len(session["tabs"]), 2)
        titles = [t["title"] for t in session["tabs"]]
        self.assertIn("配置中心", titles)
        self.assertIn("文字矫正", titles)

    def test_register_tab_dedupe_by_title(self):
        """同 title 再次注册 → 去重更新 url，tabs 数量不变。"""
        import tabmanage

        with mock.patch.object(tabmanage, "_owner_alive", return_value=True):
            tabmanage.register_tab("配置中心", "http://127.0.0.1:55044/", "http://127.0.0.1:55044/")
            role = tabmanage.register_tab("配置中心", "http://127.0.0.1:55044/new/", "http://127.0.0.1:55044/")
            self.assertEqual(role, "guest")
        with open(self._tmp_session, "r", encoding="utf-8") as f:
            session = json.load(f)
        self.assertEqual(len(session["tabs"]), 1)
        self.assertEqual(session["tabs"][0]["url"], "http://127.0.0.1:55044/new/")

    def test_register_tab_owner_dead_becomes_owner(self):
        """owner 探测失败（重试后仍失败）→ 新进程成为 owner，会话替换。"""
        import tabmanage

        with mock.patch.object(tabmanage, "_owner_alive", return_value=False):
            role = tabmanage.register_tab("配置中心", "http://127.0.0.1:55044/", "http://127.0.0.1:55044/")
            self.assertEqual(role, "owner")
        with open(self._tmp_session, "r", encoding="utf-8") as f:
            session = json.load(f)
        self.assertEqual(session["owner_pid"], os.getpid())

    def test_register_tab_owner_dead_with_existing_session_replaces(self):
        """已有会话但 owner 确认死亡（重试后仍失败）→ 接管为 owner，会话替换。"""
        import tabmanage

        with mock.patch.object(tabmanage.time, "sleep") as _sleep, mock.patch.object(
            tabmanage, "_owner_alive", return_value=False
        ) as _probe:
            # 先建 owner 会话（仅 mock sleep，保留真实 time.time 以便 JSON 序列化）
            tabmanage.register_tab("配置中心", "http://127.0.0.1:55044/", "http://127.0.0.1:55044/")
            # 新进程加入：owner 探测失败 → sleep 重试 → 仍失败 → 接管
            _sleep.reset_mock()
            _probe.reset_mock()
            role = tabmanage.register_tab("文字矫正", "http://127.0.0.1:55045/", "http://127.0.0.1:55045/")
            self.assertEqual(role, "owner")
            # 探测两次、中间 sleep 一次（重试时序）
            self.assertEqual(_probe.call_count, 2)
            self.assertEqual(_sleep.call_count, 1)
        with open(self._tmp_session, "r", encoding="utf-8") as f:
            session = json.load(f)
        self.assertEqual(session["owner_pid"], os.getpid())
        self.assertEqual(session["owner_base"], "http://127.0.0.1:55045/")
        self.assertEqual(len(session["tabs"]), 1)

    def test_register_tab_retry_rescues_transient_probe_failure(self):
        """owner 探测首个瞬态失败、重试后存活 → 仍为 guest，会话不被替换（防误抢占）。"""
        import tabmanage

        with mock.patch.object(tabmanage.time, "sleep") as _sleep, mock.patch.object(
            tabmanage, "_owner_alive", side_effect=[False, True]
        ):
            # 先建 owner 会话
            role1 = tabmanage.register_tab("配置中心", "http://127.0.0.1:55044/", "http://127.0.0.1:55044/")
            self.assertEqual(role1, "owner")
            # 再模拟新进程：首次探测瞬态失败，重试恢复
            _sleep.reset_mock()
            role2 = tabmanage.register_tab("文字矫正", "http://127.0.0.1:55045/", "http://127.0.0.1:55045/")
            self.assertEqual(role2, "guest")
            self.assertEqual(_sleep.call_count, 1)
            # 会话未被替换：owner_base 仍为原 owner、两标签都在
            with open(self._tmp_session, "r", encoding="utf-8") as f:
                session = json.load(f)
        self.assertEqual(session["owner_base"], "http://127.0.0.1:55044/")
        self.assertEqual(len(session["tabs"]), 2)
        titles = [t["title"] for t in session["tabs"]]
        self.assertIn("配置中心", titles)
        self.assertIn("文字矫正", titles)

    def test_register_tab_no_session_skips_probe(self):
        """无会话时直接成为 owner，不调用 owner 探测。"""
        import tabmanage

        with mock.patch.object(tabmanage, "_owner_alive") as _probe:
            role = tabmanage.register_tab("配置中心", "http://127.0.0.1:55044/", "http://127.0.0.1:55044/")
            self.assertEqual(role, "owner")
            _probe.assert_not_called()

    def test_reset_session_removes_file(self):
        """reset_session 删除会话文件。"""
        import tabmanage

        tabmanage.reset_session()
        self.assertFalse(os.path.exists(self._tmp_session))

    def test_guest_session_ok_true_false(self):
        """guest_session_ok：owner 存活+标题在列表 → True；否则 False。"""
        import tabmanage

        # 先建会话
        with mock.patch.object(tabmanage, "_owner_alive", return_value=True):
            tabmanage.register_tab("配置中心", "http://127.0.0.1:55044/", "http://127.0.0.1:55044/")
        # owner 存活且标题存在
        with mock.patch.object(tabmanage, "_owner_alive", return_value=True):
            self.assertTrue(tabmanage.guest_session_ok("配置中心", "http://127.0.0.1:55044/"))
        # owner 存活但标题不存在
        with mock.patch.object(tabmanage, "_owner_alive", return_value=True):
            self.assertFalse(tabmanage.guest_session_ok("不存在", "http://127.0.0.1:55044/"))
        # owner 不存活
        with mock.patch.object(tabmanage, "_owner_alive", return_value=False):
            self.assertFalse(tabmanage.guest_session_ok("配置中心", "http://127.0.0.1:55044/"))

    def test_guest_session_ok_default_uses_session_owner(self):
        """owner_base 缺省时以会话内 owner_base 探测；显式不一致 → False（rebase 守卫）。"""
        import tabmanage

        with mock.patch.object(tabmanage, "_owner_alive", return_value=True):
            tabmanage.register_tab("配置中心", "http://127.0.0.1:55044/", "http://127.0.0.1:55044/")
        # 缺省 owner_base：按会话内 owner 探测，标题存在 → True
        with mock.patch.object(tabmanage, "_owner_alive", return_value=True) as probe:
            self.assertTrue(tabmanage.guest_session_ok("配置中心"))
            probe.assert_called_once_with("http://127.0.0.1:55044/")
        # 显式传入与会话不一致的 owner_base → False（调用方不传自身 url，只传 None/确切 owner）
        with mock.patch.object(tabmanage, "_owner_alive", return_value=True):
            self.assertFalse(tabmanage.guest_session_ok("配置中心", "http://127.0.0.1:9999/"))
        # 会话内 owner 不存活 → False
        with mock.patch.object(tabmanage, "_owner_alive", return_value=False):
            self.assertFalse(tabmanage.guest_session_ok("配置中心"))

    def test_tab_host_html_returns_bytes(self):
        """/tabhost 返回 200 + HTML（monkeypatch tab_host_html 返回存根）。"""
        import tabmanage
        import guimanage

        orig_tab_host_html = tabmanage.tab_host_html
        tabmanage.tab_host_html = lambda: b"<html>tabhost stub</html>"
        try:
            status, ctype, body = self._get("/tabhost")
            self.assertEqual(status, 200)
            self.assertIn("text/html", ctype)
            self.assertIn(b"tabhost stub", body)
        finally:
            tabmanage.tab_host_html = orig_tab_host_html

    def test_api_tabs_get(self):
        """GET /api/tabs 返回 tabs 列表 + position。"""
        import tabmanage

        # 先注册两个标签
        with mock.patch.object(tabmanage, "_owner_alive", return_value=True):
            tabmanage.register_tab("配置中心", "http://127.0.0.1:55044/", "http://127.0.0.1:55044/")
            tabmanage.register_tab("文字矫正", "http://127.0.0.1:55045/", "http://127.0.0.1:55045/")
        status, _, body = self._get("/api/tabs")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["tabs"]), 2)
        self.assertIn("position", data)
        self.assertIn(data["position"], ("top", "bottom"))

    def test_api_tabs_post_position_bottom_ok(self):
        """POST /api/tabs position=bottom → 200 且 config 写入。"""
        import configmanage

        # monkeypatch config 路径到临时文件
        fd, tmp_cfg = tempfile.mkstemp(prefix="test_tab_cfg_", suffix=".json")
        os.close(fd)
        with open(tmp_cfg, "w", encoding="utf-8") as f:
            json.dump(_minimal_config(), f, ensure_ascii=False)
        orig_cfg_path = configmanage._CONFIG_PATH
        configmanage._CONFIG_PATH = tmp_cfg
        try:
            status, body = self._post("/api/tabs", {"position": "bottom"})
            self.assertEqual(status, 200)
            data = json.loads(body)
            self.assertTrue(data["ok"])
            # 验证磁盘持久化
            with open(tmp_cfg, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.assertEqual(cfg.get("tabs_position"), "bottom")
        finally:
            configmanage._CONFIG_PATH = orig_cfg_path
            try:
                os.unlink(tmp_cfg)
            except OSError:
                pass

    def test_api_tabs_post_invalid_position_400(self):
        """POST /api/tabs position=side → 400 中文错误。"""
        status, body = self._post("/api/tabs", {"position": "side"})
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertIn("tabs_position 仅支持 top / bottom", data["error"])

    def test_serve_loop_guest_session_lost_breaks(self):
        """guest 模式：会话有效时持续运行、会话丢失（guest_session_ok=False）时
        置 tab_lost 退出，绝不落入无探测的第二循环。

        回归：_serve_loop 曾被重复粘贴循环体（第二段无 guest 探测），tab_lost
        break 后落入第二段死循环 → guest 永不返回、接管失效；旧代码下本测试挂起。
        """
        import threading

        import guimanage
        import tabmanage

        state = {
            "finished": threading.Event(),
            "tab_lost": False,
            "dlg_queue": None,
        }
        calls = {"n": 0}

        def _probe_ok_then_lost(_title: str, _base: str) -> bool:
            calls["n"] += 1
            return calls["n"] == 1  # 第一次 ok，第二次 False → 触发接管

        orig_sleep = guimanage.time.sleep
        orig_drain = guimanage._drain_dialog_queue
        orig_monotonic = guimanage.time.monotonic
        orig_gso = tabmanage.guest_session_ok
        guimanage.time.sleep = lambda _s: None  # 加速循环（0.5s 步进置空）
        guimanage._drain_dialog_queue = lambda _st: None
        guimanage.time.monotonic = mock.Mock(side_effect=[0.0, 2.0, 4.0, 4.0])
        tabmanage.guest_session_ok = _probe_ok_then_lost
        try:
            guimanage._serve_loop(
                state, 600, watch_gone=False,
                tab_key="文字矫正", tab_base=None,
            )
        finally:
            guimanage.time.sleep = orig_sleep
            guimanage._drain_dialog_queue = orig_drain
            guimanage.time.monotonic = orig_monotonic
            tabmanage.guest_session_ok = orig_gso
        # 会话丢失 → tab_lost 置位退出；finished 不置（窗口仍开着，仅触发接管）
        self.assertTrue(state["tab_lost"])
        self.assertFalse(state["finished"].is_set())
        self.assertEqual(calls["n"], 2)


class TestGuiConvert(GuiServerTestBase):
    """转换流程端点测试（假 Popen，不真跑子进程）。"""

    def setUp(self):
        super().setUp()
        self._pdf = os.path.join(tempfile.gettempdir(), "test_gui_convert_sample.pdf")
        with open(self._pdf, "w", encoding="utf-8") as f:
            f.write("%PDF-1.4\n%%EOF\n")

    def tearDown(self):
        super().tearDown()
        try:
            os.unlink(self._pdf)
        except OSError:
            pass

    # -- helpers --

    def _make_fake_proc(self, lines, rc=0):
        """假 Popen：stdout 可迭代、stdin 可写、wait 返回 rc、kill 记录标志。"""
        proc = mock.Mock()
        proc.stdout = list(lines)
        proc.stdin = mock.Mock()
        proc.wait.return_value = rc
        proc.poll.return_value = None
        proc.killed = False

        def _kill():
            proc.killed = True

        proc.kill.side_effect = _kill
        return proc

    def _make_blocking_proc(self, release):
        """阻塞型假 Popen：wait 阻塞到 release，模拟长时间运行的转换。"""
        proc = mock.Mock()
        proc.stdout = iter([])
        proc.poll.return_value = None
        proc.killed = False

        def _wait(timeout=None):
            release.wait(timeout=5)
            return 0

        def _kill():
            proc.killed = True

        proc.wait.side_effect = _wait
        proc.kill.side_effect = _kill
        return proc

    def _start_body(self, **over):
        body = {"pdf": self._pdf}
        body.update(over)
        return body

    def _wait_done(self, timeout=5.0):
        """轮询 /api/convert/status 直到 done，返回状态 dict。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, _, raw = self._get("/api/convert/status")
            self.assertEqual(status, 200)
            data = json.loads(raw)
            if data.get("done"):
                return data
            time.sleep(0.05)
        self.fail("转换未在预期时间内完成")

    # -- tests --

    def test_start_validation_400(self):
        """参数校验失败 → 400，且 Popen 不被调用。"""
        cases = [
            (self._start_body(pdf=None), "缺少 PDF 文件路径"),
            (self._start_body(pdf="C:/x.txt"), "请选择 PDF 文件"),
            (self._start_body(pdf="C:/nope.pdf"), "文件不存在"),
            (self._start_body(dpi=7), "dpi 仅支持 0-4"),
            (self._start_body(dpi=True), "dpi 仅支持 0-4"),
            (self._start_body(model="NOPE"), "未知模型"),
            (self._start_body(engine="bad"), "engine 仅支持 llama / vllm"),
            (self._start_body(workers=0), "workers 必须 >= 1"),
            (self._start_body(timeout=0), "timeout 必须 >= 1"),
            (self._start_body(thinking="yes"), "thinking 必须是布尔值"),
            (self._start_body(title=123), "title 必须是字符串"),
        ]
        with mock.patch.object(
            guimanage.subprocess, "Popen", return_value=self._make_fake_proc([])
        ) as popen:
            for body, expect in cases:
                status, raw = self._post("/api/convert/start", body)
                self.assertEqual(status, 400, body)
                data = json.loads(raw)
                self.assertFalse(data["ok"])
                self.assertIn(expect, data["error"])
        popen.assert_not_called()

    def test_start_success_flow(self):
        """合法启动 → 200；完成后 done/success/epub_path/lines 正确。"""
        fake = self._make_fake_proc(
            ["[1/4] 正在识别...", "第 2 页完成", "Done: C:/tmp/out.epub"], rc=0
        )
        with mock.patch.object(
            guimanage.subprocess, "Popen", return_value=fake
        ) as popen:
            status, raw = self._post("/api/convert/start", self._start_body())
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        # argv 组装：开发环境 [python, ROOT/mian.py, epub, pdf, ...]
        argv = popen.call_args.args[0]
        self.assertEqual(argv[0], sys.executable)
        self.assertIn("epub", argv)
        self.assertIn(self._pdf, argv)
        done = self._wait_done()
        self.assertTrue(done["success"])
        self.assertEqual(done["exit_code"], 0)
        self.assertEqual(done["epub_path"], "C:/tmp/out.epub")
        self.assertTrue(any(line.startswith("[1/4]") for line in done["lines"]))
        self.assertIn("Done: C:/tmp/out.epub", done["lines"])

    def test_start_failure_flow(self):
        """子进程 rc!=0 → done 且 success=False、error 取最后几行日志。"""
        fake = self._make_fake_proc(["错误行1", "错误行2"], rc=1)
        with mock.patch.object(guimanage.subprocess, "Popen", return_value=fake):
            status, raw = self._post("/api/convert/start", self._start_body())
        self.assertEqual(status, 200)
        done = self._wait_done()
        self.assertTrue(done["done"])
        self.assertFalse(done["success"])
        self.assertEqual(done["exit_code"], 1)
        self.assertIn("错误行2", done["error"])

    # -- 弹窗询问（OCR 断点续传选择）--

    _PROMPT_MARKER_LINE = (
        '__PTOE_PROMPT__ {"id": "ocr_resume", "question": "检测到上次未完成的OCR进度（0/359页完成）。", '
        '"options": [{"value": "resume", "label": "继续识别"}, {"value": "restart", "label": "重新识别全部"}, '
        '{"value": "abort", "label": "取消"}], "default": "resume"}'
    )

    def _wait_prompt(self, timeout=5.0):
        """轮询 /api/convert/status 直到出现 prompt，返回完整状态。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, _, raw = self._get("/api/convert/status")
            self.assertEqual(status, 200)
            data = json.loads(raw)
            if data.get("prompt"):
                return data
            time.sleep(0.05)
        self.fail("未在预期时间内收到弹窗询问")

    def _make_prompt_proc(self, lines, release):
        """弹窗测试用假 Popen：stdout 逐行给标记，wait 阻塞到 release（模拟
        子进程阻塞等弹窗回答，保证 prompt 状态持续存在）。"""
        proc = mock.Mock()
        proc.stdout = list(lines)
        proc.stdin = mock.Mock()
        proc.poll.return_value = None
        proc.killed = False

        def _wait(timeout=None):
            release.wait(timeout=5)
            return 0

        def _kill():
            proc.killed = True

        proc.wait.side_effect = _wait
        proc.kill.side_effect = _kill
        return proc

    def test_convert_prompt_marker_exposed_in_status(self):
        """子进程打印 __PTOE_PROMPT__ → status.prompt 携带载荷，标记行不进日志。"""
        release = threading.Event()
        fake = self._make_prompt_proc(
            [self._PROMPT_MARKER_LINE, "第 1 页完成", "Done: C:/tmp/out.epub"], release
        )
        with mock.patch.object(guimanage.subprocess, "Popen", return_value=fake):
            status, _ = self._post("/api/convert/start", self._start_body())
        self.assertEqual(status, 200)
        data = self._wait_prompt()
        self.assertEqual(data["prompt"]["default"], "resume")
        self.assertEqual(
            [o["value"] for o in data["prompt"]["options"]],
            ["resume", "restart", "abort"],
        )
        release.set()  # 放行子进程收尾
        done = self._wait_done()
        self.assertTrue(done["success"])
        self.assertTrue(
            all("__PTOE_PROMPT__" not in ln for ln in done["lines"]),
            "标记行不应进入日志",
        )
        self.assertTrue(any("需要选择" in ln for ln in done["lines"]))
        self.assertIsNone(done.get("prompt"), "转换结束后 prompt 应清除")

    def test_convert_prompt_answer_writes_choice_to_stdin(self):
        """POST /api/convert/prompt → 选择写回子进程 stdin，prompt 清除。"""
        release = threading.Event()
        fake = self._make_prompt_proc([self._PROMPT_MARKER_LINE, "Done: C:/tmp/out.epub"], release)
        with mock.patch.object(guimanage.subprocess, "Popen", return_value=fake):
            status, _ = self._post("/api/convert/start", self._start_body())
        self.assertEqual(status, 200)
        self._wait_prompt()
        s, raw = self._post("/api/convert/prompt", {"choice": "restart"})
        self.assertEqual(s, 200, raw)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        fake.stdin.write.assert_called_with("restart\n")
        fake.stdin.flush.assert_called()
        release.set()
        _, _, raw2 = self._get("/api/convert/status")
        self.assertIsNone(json.loads(raw2).get("prompt"))

    def test_convert_prompt_answer_invalid_400(self):
        """无待答询问 / 非法选择 → 400。"""
        fake = self._make_fake_proc([], rc=0)
        with mock.patch.object(guimanage.subprocess, "Popen", return_value=fake):
            status, _ = self._post("/api/convert/start", self._start_body())
        self.assertEqual(status, 200)
        # 无 prompt 时回答 → 400
        status, raw = self._post("/api/convert/prompt", {"choice": "resume"})
        self.assertEqual(status, 400)
        self.assertIn("待回答", json.loads(raw)["error"])
        # 无 choice → 400
        status, raw = self._post("/api/convert/prompt", {})
        self.assertEqual(status, 400)
        self.assertIn("缺少选择", json.loads(raw)["error"])

    # -- 管道继承：子进程（llama-server）持有 stdout 不 EOF --

    def _make_pipe_holding_proc(self, lines, release):
        """假 Popen：stdout 输出若干行后阻塞（模拟 llama-server 继承 stdout
        管道、主进程退出后管道仍不 EOF）；poll/wait 按 release 反映进程退出。"""
        proc = mock.Mock()
        proc.stdin = mock.Mock()
        proc.killed = False

        def _gen():
            for ln in lines:
                yield ln
            # 阻塞：模拟子进程继承管道导致 stdout 永不 EOF
            release.wait(timeout=10)
            while True:
                time.sleep(3600)

        def _poll():
            return 0 if release.is_set() else None

        def _wait(timeout=None):
            release.wait(timeout=10)
            return 0

        def _kill():
            proc.killed = True

        proc.stdout = _gen()
        proc.poll.side_effect = _poll
        proc.wait.side_effect = _wait
        proc.kill.side_effect = _kill
        return proc

    def test_monitor_finalizes_when_child_holds_pipe(self):
        """转换子进程生成 epub 后退出，但 llama-server 继承 stdout 管道不 EOF：
        监控线程仍须收尾（否则按钮停在「转换中」、停止无效）。"""
        release = threading.Event()
        fake = self._make_pipe_holding_proc(
            ["第 1 页完成", "Done: C:/tmp/out.epub"], release
        )
        with mock.patch.object(guimanage.subprocess, "Popen", return_value=fake):
            status, _ = self._post("/api/convert/start", self._start_body())
        self.assertEqual(status, 200)
        # 主进程"退出"（release），但 stdout 永不 EOF
        release.set()
        done = self._wait_done(timeout=8.0)
        self.assertTrue(done["success"])
        self.assertEqual(done["exit_code"], 0)
        self.assertEqual(done["epub_path"], "C:/tmp/out.epub")
        self.assertTrue(any("第 1 页完成" in ln for ln in done["lines"]))

    def test_convert_stop_recovers_when_monitor_stuck(self):
        """监控线程卡死（stdout 永不 EOF）时，停止接口立即标记完成，
        界面能从「转换中」恢复。"""
        release = threading.Event()
        fake = self._make_pipe_holding_proc([], release)
        with mock.patch.object(guimanage.subprocess, "Popen", return_value=fake):
            status, _ = self._post("/api/convert/start", self._start_body())
        self.assertEqual(status, 200)
        time.sleep(0.3)  # 让监控线程进入等待
        s, raw = self._post("/api/convert/stop")
        self.assertEqual(s, 200)
        self.assertTrue(json.loads(raw)["ok"])
        _, _, raw2 = self._get("/api/convert/status")
        data = json.loads(raw2)
        self.assertTrue(data["done"], "停止后应立即结束（不依赖监控线程）")
        self.assertFalse(data["running"])

    def test_start_single_flight_409(self):
        """已有转换在运行 → 409「已有转换在运行」。"""
        release = threading.Event()
        proc1 = self._make_blocking_proc(release)
        try:
            with mock.patch.object(
                guimanage.subprocess, "Popen", return_value=proc1
            ) as popen:
                status, raw = self._post("/api/convert/start", self._start_body())
            self.assertEqual(status, 200)
            # 第二个请求应被拒绝
            status, raw = self._post("/api/convert/start", self._start_body())
            self.assertEqual(status, 409)
            data = json.loads(raw)
            self.assertFalse(data["ok"])
            self.assertIn("已有转换在运行", data["error"])
            popen.assert_called_once()
        finally:
            release.set()
            time.sleep(0.2)

    def test_stop_running(self):
        """停止正在运行的转换 → 200 且 kill 被调用。"""
        release = threading.Event()
        proc = self._make_blocking_proc(release)
        try:
            with mock.patch.object(guimanage.subprocess, "Popen", return_value=proc):
                status, _ = self._post("/api/convert/start", self._start_body())
            self.assertEqual(status, 200)
            status, raw = self._post("/api/convert/stop")
            self.assertEqual(status, 200)
            data = json.loads(raw)
            self.assertTrue(data["ok"])
            self.assertTrue(proc.killed)
        finally:
            release.set()
            time.sleep(0.2)

    def test_stop_no_running_400(self):
        """没有正在运行的转换 → 400。"""
        status, raw = self._post("/api/convert/stop")
        self.assertEqual(status, 400)
        data = json.loads(raw)
        self.assertFalse(data["ok"])
        self.assertIn("没有正在运行的转换", data["error"])

    def test_pick_filter_pdf(self):
        """/api/pick filter=pdf 透传到主线程弹框。"""
        captured = {}

        def fake_drain(state):
            while True:
                try:
                    req = state["dlg_queue"].get_nowait()
                except queue.Empty:
                    time.sleep(0.01)
                    continue
                captured.update(req)
                req["result"] = {"ok": True, "path": "C:/x.pdf"}
                req["done"].set()
                return

        orig = guimanage._drain_dialog_queue
        guimanage._drain_dialog_queue = fake_drain
        drainer = threading.Thread(
            target=lambda: guimanage._drain_dialog_queue(self._state), daemon=True
        )
        try:
            drainer.start()
            status, raw = self._post("/api/pick", {"kind": "file", "filter": "pdf"})
        finally:
            guimanage._drain_dialog_queue = orig
            drainer.join(timeout=5)
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertEqual(data["path"], "C:/x.pdf")
        self.assertEqual(captured.get("filter"), "pdf")

    def test_pick_filter_invalid_400(self):
        """/api/pick filter 非法 → 400。"""
        status, raw = self._post("/api/pick", {"kind": "file", "filter": "exe"})
        self.assertEqual(status, 400)
        data = json.loads(raw)
        self.assertFalse(data["ok"])
        self.assertIn("filter", data["error"])


class TestConvertArgv(unittest.TestCase):
    """_convert_argv 组装逻辑（不依赖 HTTP 服务）。"""

    def test_dev_argv(self):
        """开发环境：python -u + ROOT/mian.py + epub + 参数。"""
        argv = guimanage._convert_argv(
            "a.pdf",
            dpi=2,
            model="HY",
            engine="vllm",
            workers=5,
            timeout=300,
            thinking=True,
            title="书名",
            author="作者",
            lang="zh-CN",
            out_dir="o",
            epub_path="e.epub",
        )
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1], "-u")
        self.assertTrue(argv[2].endswith("mian.py"))
        self.assertEqual(argv[3], "epub")
        self.assertEqual(argv[4], "a.pdf")
        self.assertIn("--dpi", argv)
        self.assertEqual(argv[argv.index("--dpi") + 1], "2")
        self.assertEqual(argv[argv.index("--model") + 1], "HY")
        self.assertEqual(argv[argv.index("--engine") + 1], "vllm")
        self.assertEqual(argv[argv.index("--workers") + 1], "5")
        self.assertEqual(argv[argv.index("--timeout") + 1], "300")
        self.assertIn("--thinking", argv)
        self.assertEqual(argv[argv.index("--title") + 1], "书名")
        self.assertEqual(argv[argv.index("--author") + 1], "作者")
        self.assertEqual(argv[argv.index("--lang") + 1], "zh-CN")
        self.assertEqual(argv[argv.index("--out-dir") + 1], "o")
        self.assertEqual(argv[argv.index("--epub-path") + 1], "e.epub")

    def test_dev_argv_defaults_skipped(self):
        """None/空值参数不进入 argv（走 CLI 默认值）。"""
        argv = guimanage._convert_argv("a.pdf")
        self.assertEqual(argv, [sys.executable, "-u", os.path.join(guimanage.ROOT, "mian.py"), "epub", "a.pdf"])
        self.assertNotIn("--dpi", argv)

    def test_frozen_argv(self):
        """冻结 exe：argv 以自身为可执行文件（无 mian.py 路径）。"""
        with mock.patch.object(sys, "frozen", True, create=True):
            argv = guimanage._convert_argv("a.pdf", dpi=0, thinking=True)
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1], "epub")
        self.assertEqual(argv[2], "a.pdf")
        # dpi=0 且 thinking 传值
        self.assertEqual(argv[argv.index("--dpi") + 1], "0")
        self.assertIn("--thinking", argv)
        self.assertNotIn("mian.py", argv)


class TestMianWiring(unittest.TestCase):
    """mian.py 的 gui 子命令与终端菜单第 8 项接线。"""

    def setUp(self):
        # 菜单/help 路径会调 get_config()：monkeypatch 到临时文件，避免触碰真实 config.json
        fd, self._cfg_path = tempfile.mkstemp(prefix="test_gui_mian_", suffix=".json")
        os.close(fd)
        cfg = _minimal_config()
        # 指向真实存在的路径，避免 get_config(show_dialogs=True) 弹 tkinter 对话框
        cfg["llama_server"] = str(sys.executable)
        cfg["models_dir"] = tempfile.mkdtemp(prefix="test_gui_mdir_")
        with open(self._cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        self._orig_cfg_path = configmanage._CONFIG_PATH
        configmanage._CONFIG_PATH = self._cfg_path

    def tearDown(self):
        configmanage._CONFIG_PATH = self._orig_cfg_path
        try:
            os.unlink(self._cfg_path)
        except OSError:
            pass

    def test_gui_help_exits_zero(self):
        """mian.main(['gui', '--help']) → SystemExit(0)。"""
        with self.assertRaises(SystemExit) as cm:
            mian.main(["gui", "--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_menu_8_calls_gui_serve(self):
        """终端菜单输入 '8' 时调用 guimanage.gui_serve（mian 惰性 import）。"""
        import contextlib
        import io

        class _TTY(io.StringIO):
            def isatty(self):
                return True

        old_stdin = sys.stdin
        sys.stdin = _TTY("8\n0\n")
        try:
            with mock.patch("guimanage.gui_serve") as gs:
                with contextlib.redirect_stdout(io.StringIO()) as buf:
                    rc = mian.main([])
            out = buf.getvalue()
        finally:
            sys.stdin = old_stdin
        self.assertEqual(rc, 0)
        gs.assert_called_once()
        self.assertIn("请选择操作", out)
        self.assertIn("配置界面", out)


if __name__ == "__main__":
    unittest.main()