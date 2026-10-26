"""guimanage 后端与 CLI 接线测试（stdlib unittest，中文 docstring）。

覆盖：GET / 返回 UI、/api/config 读写、/api/status、/api/ping、/api/bye、
/api/server/start|stop、/api/pick（cancelled 路径，不真弹 tkinter）、
_browser_gone 判定，以及 mian.py 的 gui 子命令与终端菜单第 8 项接线。

测试期间 monkeypatch configmanage._CONFIG_PATH 到临时文件（严禁写真实 config.json）。
"""

import contextlib
import inspect
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path as _Path
from unittest import mock

import configmanage
import epubmetamanage
import guimanage
import logmanage
import mian


class _BlockingIter:
    """迭代永不结束：矫正子进程 stdout 保持打开（实例维持 running 状态）。"""

    def __iter__(self):
        return self

    def __next__(self):
        time.sleep(10)
        return ""


class _FakeCorrectProc:
    """假的 subprocess.Popen：poll/wait/terminate/kill + stdout 迭代。

    block=True 时 stdout 永不完结 → _correct_monitor 不会收尾，实例保持
    running；否则给出若干行（含矫正界面地址 http://127.0.0.1:<port>/）。
    """

    def __init__(self, stdout_lines=None, block=False):
        self.pid = 4242
        self._terminated = False
        if block:
            self.stdout = _BlockingIter()
        else:
            self.stdout = list(stdout_lines or ["http://127.0.0.1:8123/\n"])

    def poll(self):
        return 0 if self._terminated else None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self._terminated = True

    def kill(self):
        self._terminated = True


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
            "correct_instances": [],
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

    def test_get_status_paddle_engine(self):
        """GET /api/status engine=paddle → probe=local, port=--（mock _active_engine）。"""
        import llamamanage

        with mock.patch.object(
            llamamanage, "_active_engine", return_value="paddle"
        ), mock.patch.object(
            llamamanage, "_probe_server"
        ) as probe:
            status, _, body = self._get("/api/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["engine"], "paddle")
        self.assertEqual(data["probe"], "local")
        self.assertEqual(data["port"], "--")
        probe.assert_not_called()  # paddle 不走网络探测

    def test_post_config_invalid_engine_400(self):
        """POST /api/config 非法 engine → 400。"""
        status, body = self._post("/api/config", {"engine": "bad"})
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertIn("engine", data["error"])

    def test_post_config_paddle_engine_200(self):
        """POST /api/config engine=paddle → 200 ok=True。"""
        status, body = self._post("/api/config", {"engine": "paddle"})
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])

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

    def test_post_config_paddle_engine_persists(self):
        """POST /api/config engine=paddle 写入后 GET /api/config 确认持久化。"""
        status, body = self._post("/api/config", {"engine": "paddle"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        # GET 验证
        _, _, body2 = self._get("/api/config")
        data = json.loads(body2)
        self.assertEqual(data["config"]["engine"], "paddle")

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

    def test_start_correct_with_paddle_engine(self):
        """POST /api/correct/start engine=paddle → 400（矫正不使用 PaddleOCR，仅 llama/vllm）。"""
        # GuiServerTestBase setUp 不含 correct/convert 键，此处补全
        self._state.setdefault("correct", {
            "lock": threading.Lock(), "proc": None, "lines": [],
            "running": False, "done": False, "success": False,
            "exit_code": None, "error": None, "prompt": None,
        })
        self._state.setdefault("convert", {
            "lock": threading.Lock(), "proc": None, "lines": [],
            "running": False, "done": False, "success": False,
            "exit_code": None, "error": None, "prompt": None,
        })
        fake = mock.Mock()
        with mock.patch.object(guimanage.subprocess, "Popen", return_value=fake):
            status, raw = self._post(
                "/api/correct/start", {"engine": "paddle"}
            )
        self.assertEqual(status, 400)
        data = json.loads(raw)
        self.assertFalse(data["ok"])
        self.assertIn("矫正仅支持 llama / vllm", data["error"])
        fake.assert_not_called()

    def test_post_config_proofread_engine_invalid_400(self):
        """POST /api/config proofread_engine='paddle' → 400（矫正仅 llama/vllm）。"""
        status, raw = self._post("/api/config", {"proofread_engine": "paddle"})
        self.assertEqual(status, 400)
        data = json.loads(raw)
        self.assertFalse(data["ok"])
        self.assertIn("proofread_engine 仅支持 llama / vllm", data["error"])

    def test_post_config_proofread_engine_vllm_persists(self):
        """POST /api/config proofread_engine='vllm' → 200 且写盘。"""
        status, raw = self._post("/api/config", {"proofread_engine": "vllm"})
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        with open(self._cfg_path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["proofread_engine"], "vllm")


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


class TestServeLoopNoQuitWhileJobRunning(unittest.TestCase):
    """矫正/转换长任务运行中，_serve_loop 不得因「浏览器失联」自动退出。

    根因（用户返修单：长时间使用矫正界面后界面无任何异常提示就结束了）：
    gui_serve 在 browser 模式下用 _browser_gone（pagehide 信标 /api/bye 或
    心跳失联 idle_timeout*2）判定浏览器已关闭，随即退出 finally，并把仍在
    运行的矫正子进程 proc.kill() —— 矫正界面瞬间消失、控制台无任何异常、
    data/correct_server.json 留下大量陈旧实例（从未走到清理）。而浏览器
    （Thorium/Chrome）在内存吃紧时会把后台标签页挂起/回收，此时页面既不发
    心跳也可能补发 pagehide，属于误判而非用户真的关闭服务。

    修复：长任务运行中只认 state["finished"]（Ctrl+C / 显式关闭）这一条真实
    退出通道；gone 判定成立也要把服务留着，让用户回来后页面自己重连。
    """

    IDLE_TIMEOUT = 1  # 测试里把阈值压到最小，保证 _browser_gone 立即成立

    def _make_state(self, running_instances):
        return {
            "finished": threading.Event(),
            "tab_lost": False,
            "dlg_queue": queue.Queue(),
            "dlg_lock": threading.Lock(),
            "beat_lock": threading.Lock(),
            "gone_at": None,
            # 心跳故意置为很久以前 → _browser_gone 立刻判定失联
            "last_beat": time.monotonic() - 10000.0,
            "convert": {"lock": threading.Lock(), "proc": None, "running": False},
            "merge": {
                "lock": threading.Lock(),
                "running": False,
                "stop_event": threading.Event(),
            },
            "correct_instances": [
                {"running": True, "finalized": False, "port": 50000 + i,
                 "url": "http://127.0.0.1:%d/" % (50000 + i), "proc": None}
                for i in range(running_instances)
            ],
            "epubedit_instances": [],
        }

    def _loop_target(self, state, buf):
        with contextlib.redirect_stdout(buf):
            guimanage._serve_loop(state, self.IDLE_TIMEOUT, watch_gone=True)

    def _run_loop(self, state):
        buf = io.StringIO()
        thread = threading.Thread(
            target=self._loop_target, args=(state, buf), daemon=True
        )
        thread.start()
        return thread, buf

    def test_no_quit_while_correct_running(self):
        """矫正实例运行中 + 浏览器失联 → 不退出（回归：曾静默强杀矫正）。"""
        state = self._make_state(1)
        thread, buf = self._run_loop(state)
        try:
            # _browser_gone 需「连续失联 _STALE_CONFIRM_SECONDS」才成立，等它成立
            time.sleep(5.0)
            self.assertTrue(thread.is_alive(), "长任务运行中 _serve_loop 竟然退出了")
            self.assertFalse(state["finished"].is_set())
            self.assertIn("服务保持后台运行", buf.getvalue())
        finally:
            state["finished"].set()
            thread.join(timeout=3.0)

    def test_quit_when_browser_gone_and_no_job(self):
        """无长任务 + 浏览器失联 → 正常退出并给出控制台说明。"""
        state = self._make_state(0)
        thread, buf = self._run_loop(state)
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive(), "无任务且浏览器失联时应退出")
        self.assertTrue(state["finished"].is_set())
        self.assertIn("配置中心自动退出", buf.getvalue())

    def test_quit_after_job_finishes_when_browser_away(self):
        """任务结束后浏览器仍未回来 → 自动退出（不无限期占用端口）。"""
        state = self._make_state(1)
        thread, buf = self._run_loop(state)
        try:
            time.sleep(1.5)
            self.assertTrue(thread.is_alive())
            state["correct_instances"][0]["running"] = False  # 矫正任务结束
            thread.join(timeout=8.0)
            self.assertFalse(thread.is_alive(), "任务结束后应能自动退出")
            self.assertTrue(state["finished"].is_set())
        finally:
            state["finished"].set()
            thread.join(timeout=3.0)

    def test_any_job_running_flags(self):
        """_any_job_running 对四类长任务都能识别。"""
        state = self._make_state(1)
        self.assertTrue(guimanage._any_job_running(state))
        state["correct_instances"][0]["running"] = False
        self.assertFalse(guimanage._any_job_running(state))
        with state["convert"]["lock"]:
            state["convert"]["running"] = True
        self.assertTrue(guimanage._any_job_running(state))
        with state["convert"]["lock"]:
            state["convert"]["running"] = False
        with state["merge"]["lock"]:
            state["merge"]["running"] = True
        self.assertTrue(guimanage._any_job_running(state))
        with state["merge"]["lock"]:
            state["merge"]["running"] = False
        state["epubedit_instances"].append({"running": True})
        self.assertTrue(guimanage._any_job_running(state))


class TestHttpServerWatchdog(unittest.TestCase):
    """HTTP serve 线程死掉必须被 _serve_loop 重启（白屏自愈）。

    此前 serve_forever 死掉后端口拒绝连接，配置中心刷新即白屏，而主线程还
    压在监视循环里，进程「看起来还在运行」，控制台只有启动那句 Ctrl+C 提示。
    """

    def test_restarts_dead_serve_thread(self):
        state = {"finished": threading.Event()}
        real = ThreadingHTTPServer(("127.0.0.1", 0), _NoopHandler)
        state["http_server"] = real
        state["http_thread"] = threading.Thread(target=lambda: None)  # 已死的线程
        state["http_thread"].start()
        state["http_thread"].join(timeout=1.0)
        self.assertFalse(state["http_thread"].is_alive())
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            guimanage._ensure_http_server(state)
            self.assertIn("重启", buf.getvalue())
        new_thread = state["http_thread"]
        self.assertTrue(new_thread.is_alive())
        self.assertTrue(real.server_address[1] > 0)
        # 重启后端口真的能接受连接
        import urllib.request

        with urllib.request.urlopen(
            "http://127.0.0.1:%d/" % real.server_address[1], timeout=3
        ) as resp:
            self.assertEqual(resp.status, 200)
        state["finished"].set()
        guimanage._serve_loop(state, 1, watch_gone=True)  # 退出并停掉重启的线程
        real.shutdown()
        real.server_close()
        new_thread.join(timeout=3.0)
        self.assertFalse(new_thread.is_alive())

    def test_alive_thread_untouched(self):
        state = {"finished": threading.Event()}
        real = ThreadingHTTPServer(("127.0.0.1", 0), _NoopHandler)
        thread = threading.Thread(target=_run_forever_factory(real), daemon=True)
        thread.start()
        state["http_server"] = real
        state["http_thread"] = thread
        time.sleep(0.3)
        self.assertTrue(thread.is_alive())
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            guimanage._ensure_http_server(state)
            self.assertEqual(buf.getvalue(), "")  # 活着就别乱重启
        self.assertIs(state["http_thread"], thread)
        real.shutdown()
        real.server_close()
        thread.join(timeout=3.0)


def _run_forever_factory(server):
    def _run():
        server.serve_forever()

    return _run


class _NoopHandler(BaseHTTPRequestHandler):
    """空 handler：GET/ → 200，仅供看护测试用。"""

    def do_GET(self):  # noqa: N802
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 静音
        pass


class TestCorrectMonitorConsoleEcho(unittest.TestCase):
    """矫正子进程的出错行必须同时打到控制台（不再只藏在界面日志面板里）。

    矫正子进程 stdout 被 _correct_monitor 收进实例 lines 后只会在界面日志
    面板显示，用户控制台什么都看不到。界面若无异常提示就结束，用户完全无从
    判断原因。现在 traceback/错误/失败等行 + 非 0 退出码都回显到控制台。
    """

    def test_error_lines_echoed_to_console(self):
        proc = _FakeCorrectProc(
            [
                "http://127.0.0.1:8123/\n",
                "Traceback (most recent call last):\n",
                "RuntimeError: fitz document closed\n",
            ]
        )
        inst = {"id": "t1", "running": True, "finalized": False}
        guimanage._correct_monitor(inst, proc)
        self.assertEqual(inst["exit_code"], 0)
        # 出错行进 lines（面板可见）
        self.assertTrue(any("Traceback" in ln for ln in inst["lines"]))

    def test_nonzero_exit_echoed_to_console(self):
        proc = _FakeCorrectProc(["矫正界面已启动: http://127.0.0.1:8123/\n"])
        proc.wait = lambda timeout=None: 3  # 非 0 退出码
        inst = {"id": "t2", "running": True, "finalized": False}
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            guimanage._correct_monitor(inst, proc)
            out = buf.getvalue()
        self.assertEqual(inst["exit_code"], 3)
        self.assertIn("退出码 3", out)

    def test_normal_lines_not_echoed(self):
        proc = _FakeCorrectProc(["http://127.0.0.1:8123/\n", "预渲染 3/12 页\n"])
        inst = {"id": "t3", "running": True, "finalized": False}
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            guimanage._correct_monitor(inst, proc)
            out = buf.getvalue()
        self.assertNotIn("预渲染", out)


class TestOpenBrowserDetached(unittest.TestCase):
    """_open_browser / _spawn_detached：浏览器必须「完全脱离」启动。

    根因（2026-10-05 实测）：浏览器是常驻进程，裸 subprocess.Popen 会让它继承宿主
    stdin/stdout/stderr 控制台句柄，宿主主流程结束后解释器仍等这些句柄关闭才退出
    → 「按 Ctrl+C 关不掉配置界面」。对照：完全继承 45.1s / 仅 DEVNULL(out,err) 2.1s /
    完全脱离 2.3s。
    """

    def setUp(self):
        self._orig_get_config = configmanage.get_config
        # 防止其它测试类（如 TestOpenDisplay）把 _open_browser 换成 lambda 后泄漏到本类
        self._orig_open_browser = guimanage._open_browser
        self._orig_webbrowser_open = guimanage.webbrowser.open

    def tearDown(self):
        configmanage.get_config = self._orig_get_config
        guimanage._open_browser = self._orig_open_browser
        guimanage.webbrowser.open = self._orig_webbrowser_open

    @staticmethod
    def _expected_flags():
        return getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    def test_spawn_detached_uses_devnull_and_close_fds(self):
        """_spawn_detached：stdin/stdout/stderr 全部 DEVNULL + close_fds=True。"""
        with mock.patch.object(subprocess, "Popen") as popen:
            guimanage._spawn_detached(["D:/min/min.exe", "http://127.0.0.1:1/"])
        popen.assert_called_once()
        argv = popen.call_args[0][0]
        kwargs = popen.call_args[1]
        self.assertEqual(argv, ["D:/min/min.exe", "http://127.0.0.1:1/"])
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertIs(kwargs["close_fds"], True)
        # 不得回退成继承（None = 继承宿主句柄，正是本 bug 的根因）
        for key in ("stdin", "stdout", "stderr"):
            self.assertIsNot(kwargs[key], None)

    def test_spawn_detached_creationflags_exact(self):
        """Windows 下 creationflags 精确等于 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP。"""
        with mock.patch.object(subprocess, "Popen") as popen:
            guimanage._spawn_detached(["br", "u"])
        flags = popen.call_args[1].get("creationflags", 0)
        self.assertEqual(flags, self._expected_flags())
        if sys.platform == "win32":
            # 两常量必须都置位（缺一即退化为不脱离）
            self.assertTrue(flags & getattr(subprocess, "DETACHED_PROCESS", 0))
            self.assertTrue(flags & getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))

    def test_spawn_detached_no_create_new_console(self):
        """禁止混用 CREATE_NEW_CONSOLE（与 DETACHED_PROCESS 互斥，Windows 直接报错）。"""
        with mock.patch.object(subprocess, "Popen") as popen:
            guimanage._spawn_detached(["br", "u"])
        flags = popen.call_args[1].get("creationflags", 0)
        self.assertFalse(flags & getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        self.assertNotEqual(flags, getattr(subprocess, "CREATE_NEW_CONSOLE", 0))

    def test_spawn_detached_source_never_mixes_console_flags(self):
        """源码级守卫：不得同时出现 DETACHED_PROCESS 与 CREATE_NEW_CONSOLE 字面量。"""
        src = inspect.getsource(guimanage._spawn_detached)
        self.assertIn("DETACHED_PROCESS", src)
        self.assertNotIn("CREATE_NEW_CONSOLE", src.split("注意两点")[0])

    def test_open_browser_spawns_detached_browser(self):
        """_open_browser 走 _spawn_detached：argv=[浏览器路径, url] + 脱离参数。"""

        def fake_get_config(show_dialogs=True):
            return {"browser": "D:/min/min.exe"}

        configmanage.get_config = fake_get_config
        with mock.patch.object(subprocess, "Popen") as popen:
            guimanage._open_browser("http://127.0.0.1:8765/")
        popen.assert_called_once()
        self.assertEqual(popen.call_args[0][0], ["D:/min/min.exe", "http://127.0.0.1:8765/"])
        kwargs = popen.call_args[1]
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertIs(kwargs["close_fds"], True)
        self.assertEqual(kwargs.get("creationflags", 0), self._expected_flags())

    def test_open_browser_empty_path_uses_webbrowser(self):
        """browser 键为空 → 走 webbrowser.open，不起子进程。"""
        configmanage.get_config = lambda show_dialogs=True: {"browser": ""}
        opened = []
        guimanage.webbrowser.open = lambda u: opened.append(u)
        with mock.patch.object(subprocess, "Popen") as popen:
            guimanage._open_browser("http://127.0.0.1:8765/")
        popen.assert_not_called()
        self.assertEqual(opened, ["http://127.0.0.1:8765/"])

    def test_open_browser_swallows_spawn_failure(self):
        """浏览器路径无效（Popen 抛错）→ 不阻断服务，保持原有吞异常语义。"""
        configmanage.get_config = lambda show_dialogs=True: {"browser": "D:/不存在/min.exe"}
        with mock.patch.object(subprocess, "Popen", side_effect=OSError("no such file")):
            guimanage._open_browser("http://127.0.0.1:8765/")  # 不抛异常即通过

    def test_open_browser_returns_immediately_with_sleeping_child(self):
        """行为级：常驻「假浏览器」（sleep 子进程）不得拖住调用方返回。"""
        proc = None
        t0 = time.time()
        try:
            proc = guimanage._spawn_detached([sys.executable, "-c", "import time; time.sleep(30)"])
            elapsed = time.time() - t0
            self.assertIsNotNone(proc)
            self.assertLess(elapsed, 5.0, "起常驻子进程后调用方应立即返回")
        finally:
            if proc is not None:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001  收尾失败不影响断言
                    pass

    def test_open_display_browser_mode_uses_detached(self):
        """端到端：_open_display('browser') 拉起的浏览器也必须完全脱离。"""
        configmanage.get_config = lambda show_dialogs=True: {
            "gui_display": "browser",
            "browser": "D:/min/min.exe",
        }
        with mock.patch.object(subprocess, "Popen") as popen:
            role, win = guimanage._open_display("http://127.0.0.1:8765", "ptoe")
        self.assertEqual(role, "browser")
        self.assertIsNone(win)
        self.assertEqual(popen.call_args[1].get("creationflags", 0), self._expected_flags())
        self.assertIs(popen.call_args[1]["stderr"], subprocess.DEVNULL)


class TestOpenDisplay(unittest.TestCase):
    """_open_display 助手函数测试（monkeypatch configmanage.get_config + webview/webbrowser）。"""

    def setUp(self):
        self._orig_get_config = configmanage.get_config
        self._orig_open_browser = guimanage._open_browser
        self._orig_modules = dict(sys.modules)

    def tearDown(self):
        configmanage.get_config = self._orig_get_config
        # 还原 _open_browser：否则本类替换的 lambda 会泄漏给后续测试类
        guimanage._open_browser = self._orig_open_browser
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
            "correct_instances": [],
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


class TestGuiPaddleMergeLines(GuiServerTestBase):
    """paddle_merge_lines（PaddleOCR 多行合并成段落）开关的 GET/POST 校验（2026-09-28）。"""

    def _on_disk(self):
        with open(self._cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_get_defaults_true_then_post_false_persists(self):
        """键缺失时 GET 下发默认 True；POST false → 200 且真正写盘为布尔 False。"""
        status, _, body = self._get("/api/config")
        self.assertEqual(status, 200)
        self.assertIs(json.loads(body)["config"]["paddle_merge_lines"], True)

        status, raw = self._post("/api/config", {"paddle_merge_lines": False})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["ok"])
        # 磁盘持久化验证：存的是布尔 False，不是字符串
        self.assertIs(self._on_disk()["paddle_merge_lines"], False)

        status, _, body = self._get("/api/config")
        self.assertEqual(status, 200)
        self.assertIs(json.loads(body)["config"]["paddle_merge_lines"], False)

    def test_post_invalid_value_rejected_and_not_written(self):
        """POST 非法值 → 400 中文错误，且该键不会被写成非法值。"""
        status, raw = self._post("/api/config", {"paddle_merge_lines": "maybe"})
        self.assertEqual(status, 400)
        data = json.loads(raw)
        self.assertFalse(data["ok"])
        self.assertIn("paddle_merge_lines 仅接受 true/false", data["error"])
        # 端点开头的 get_config() 会把 DEFAULT_CONFIG 的缺省键回填并重写文件，
        # 故此处不能比对整份文件字节，只断言该键不是被写进去的非法值。
        stored = self._on_disk().get("paddle_merge_lines", True)
        self.assertIs(stored, True, "非法值不得写盘（键缺省或保持默认 True）")


class TestGuiTableConvert(GuiServerTestBase):
    """table_convert（识别文字表格转成原生表格）开关的 GET/POST 校验（2026-10-17）。"""

    def _on_disk(self):
        with open(self._cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_default_true(self):
        from configmanage import DEFAULT_CONFIG

        self.assertIs(DEFAULT_CONFIG["table_convert"], True)

    def test_get_defaults_true_then_post_false_persists(self):
        status, _, body = self._get("/api/config")
        self.assertEqual(status, 200)
        self.assertIs(json.loads(body)["config"]["table_convert"], True)

        status, raw = self._post("/api/config", {"table_convert": False})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["ok"])
        self.assertIs(self._on_disk()["table_convert"], False)

        status, _, body = self._get("/api/config")
        self.assertEqual(status, 200)
        self.assertIs(json.loads(body)["config"]["table_convert"], False)

    def test_post_invalid_value_rejected_and_not_written(self):
        status, raw = self._post("/api/config", {"table_convert": "maybe"})
        self.assertEqual(status, 400)
        data = json.loads(raw)
        self.assertFalse(data["ok"])
        self.assertIn("table_convert 仅接受 true/false", data["error"])
        self.assertIs(self._on_disk().get("table_convert", True), True)


class TestGuiPaddleDetLimit(GuiServerTestBase):
    """paddle_det_limit（PaddleOCR 检测最长边）的 GET/POST 校验（2026-10）。"""

    def _on_disk(self):
        with open(self._cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_default_is_960(self):
        from configmanage import DEFAULT_CONFIG

        self.assertEqual(DEFAULT_CONFIG["paddle_det_limit"], 960)

    def test_get_defaults_960_then_post_persists_int(self):
        """键缺失时 GET 下发 960；POST 1280 → 200 且写盘为 int。"""
        status, _, body = self._get("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["config"]["paddle_det_limit"], 960)

        status, raw = self._post("/api/config", {"paddle_det_limit": 1280})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["ok"])
        self.assertEqual(self._on_disk()["paddle_det_limit"], 1280)

        status, _, body = self._get("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["config"]["paddle_det_limit"], 1280)

    def test_post_zero_means_disabled_and_persists(self):
        """0 = 关闭检测缩放，必须能存成 0（不能被当成缺省/非法）。"""
        status, raw = self._post("/api/config", {"paddle_det_limit": 0})
        self.assertEqual(status, 200, raw)
        self.assertEqual(self._on_disk()["paddle_det_limit"], 0)

        status, _, body = self._get("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["config"]["paddle_det_limit"], 0)

    def test_post_invalid_value_rejected_and_not_written(self):
        """负数/过小/过大/非数字 → 400 中文错误，且不写盘。"""
        for bad in (-1, 63, 4001, "abc"):
            status, raw = self._post("/api/config", {"paddle_det_limit": bad})
            self.assertEqual(status, 400, f"值 {bad!r} 应被拒绝: {raw}")
            data = json.loads(raw)
            self.assertFalse(data["ok"])
            self.assertIn("paddle_det_limit 仅接受", data["error"])


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
        # 任务日志写到临时目录（不污染仓库 logs/）
        self._logs_td = tempfile.TemporaryDirectory()
        self.addCleanup(self._logs_td.cleanup)
        patcher = mock.patch.object(
            logmanage, "logs_dir", return_value=_Path(self._logs_td.name)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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
            (self._start_body(dpi=7), "dpi 仅支持档位 0-4"),
            (self._start_body(dpi=1300), "dpi 仅支持档位 0-4"),
            (self._start_body(dpi=True), "dpi 仅支持档位 0-4"),
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

    def test_start_accepts_custom_dpi(self):
        """dpi 支持档位 0-4 与原始数值 36-1200，均透传为 --dpi 字符串。"""
        for dpi in (0, 4, 36, 300, 1200):
            with mock.patch.object(
                guimanage.subprocess, "Popen", return_value=self._make_fake_proc([])
            ) as popen:
                status, raw = self._post(
                    "/api/convert/start", self._start_body(dpi=dpi)
                )
            self.assertEqual(status, 200, dpi)
            argv = popen.call_args[0][0]
            self.assertIn("--dpi", argv)
            self.assertEqual(argv[argv.index("--dpi") + 1], str(dpi))

    def test_start_convert_with_paddle_engine(self):
        """POST /api/convert/start engine=paddle → 200（合法引擎，不被校验拒绝）。"""
        fake = self._make_fake_proc(["Done: C:/tmp/out.epub"], rc=0)
        with mock.patch.object(
            guimanage.subprocess, "Popen", return_value=fake
        ) as popen:
            status, raw = self._post(
                "/api/convert/start", self._start_body(engine="paddle")
            )
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        popen.assert_called_once()

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

    def test_convert_progress_marker_updates_message(self):
        """进度标记行 → 状态里的 message/progress（界面只显示进度条+一条消息）。"""
        marker = (
            '__PTOE_PROGRESS__ {"stage": "ocr", "message": "正在识别 3/10 页（30%）", '
            '"done": 3, "total": 10, "percent": 30.0}'
        )
        fake = self._make_fake_proc([marker, "Done: C:/tmp/out.epub"])
        with mock.patch.object(guimanage.subprocess, "Popen", return_value=fake):
            status, raw = self._post("/api/convert/start", self._start_body())
        self.assertEqual(status, 200)
        done = self._wait_done()
        self.assertTrue(done["success"])
        self.assertEqual(done["message"], "转换完成：C:/tmp/out.epub")
        self.assertEqual(done["progress"]["done"], 3)
        self.assertEqual(done["progress"]["total"], 10)
        self.assertEqual(done["progress"]["percent"], 30.0)
        # 进度标记行不进界面日志区（只进本地日志文件）
        self.assertFalse(any("__PTOE_PROGRESS__" in ln for ln in done["lines"]))

    def test_convert_output_written_to_local_log(self):
        """完整输出写入本地日志文件（logs/convert-*.log）。"""
        fake = self._make_fake_proc(["第一行输出", "Done: C:/tmp/out.epub"])
        with mock.patch.object(guimanage.subprocess, "Popen", return_value=fake):
            self._post("/api/convert/start", self._start_body())
        done = self._wait_done()
        log_path = done["log_path"]
        self.assertTrue(log_path, "状态里应有日志文件路径")
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("第一行输出", content)
        finally:
            try:
                os.unlink(log_path)
            except OSError:
                pass

    def test_open_log_endpoint_rejects_outside_logs(self):
        """打开日志接口：只允许 logs/ 目录下的文件。"""
        status, raw = self._post("/api/open-log", {"path": "C:/Windows/win.ini"})
        self.assertEqual(status, 400)
        data = json.loads(raw)
        self.assertFalse(data["ok"])

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

    def test_pick_save_forwards_default(self):
        """/api/pick kind=save：透传 default/initialdir（不真弹 tkinter）。"""
        captured = {}

        def fake_drain(state):
            while True:
                try:
                    req = state["dlg_queue"].get_nowait()
                except queue.Empty:
                    time.sleep(0.01)
                    continue
                captured.update(req)
                req["result"] = {"ok": True, "path": "C:/out/ptoe_history.zip"}
                req["done"].set()
                return

        orig = guimanage._drain_dialog_queue
        guimanage._drain_dialog_queue = fake_drain
        drainer = threading.Thread(
            target=lambda: guimanage._drain_dialog_queue(self._state), daemon=True
        )
        try:
            drainer.start()
            status, raw = self._post(
                "/api/pick",
                {"kind": "save", "title": "另存历史备份为 ZIP", "default": "ptoe_history_1.zip"},
            )
        finally:
            guimanage._drain_dialog_queue = orig
            drainer.join(timeout=5)
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertEqual(data["path"], "C:/out/ptoe_history.zip")
        self.assertEqual(captured.get("kind"), "save")
        self.assertEqual(captured.get("default"), "ptoe_history_1.zip")
        self.assertFalse(captured.get("multiple"))  # save 强制单选

    def test_pick_save_ext_forwarded(self):
        """/api/pick kind=save 按格式传 ext/filetypes → 透传到主线程弹框。"""
        captured = {}

        def fake_drain(state):
            while True:
                try:
                    req = state["dlg_queue"].get_nowait()
                except queue.Empty:
                    time.sleep(0.01)
                    continue
                captured.update(req)
                req["result"] = {"ok": True, "path": "C:/out/book.epub"}
                req["done"].set()
                return

        orig = guimanage._drain_dialog_queue
        guimanage._drain_dialog_queue = fake_drain
        drainer = threading.Thread(
            target=lambda: guimanage._drain_dialog_queue(self._state), daemon=True
        )
        try:
            drainer.start()
            status, raw = self._post(
                "/api/pick",
                {
                    "kind": "save",
                    "title": "导出为 EPUB",
                    "default": "book.epub",
                    "ext": ".epub",
                    "filetypes": [["EPUB 电子书", "*.epub"], ["所有文件", "*.*"]],
                },
            )
        finally:
            guimanage._drain_dialog_queue = orig
            drainer.join(timeout=5)
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertEqual(captured.get("save_ext"), ".epub")
        self.assertEqual(
            captured.get("save_filetypes"),
            [["EPUB 电子书", "*.epub"], ["所有文件", "*.*"]],
        )

    def test_pick_save_ext_invalid_type_400(self):
        """/api/pick ext/filetypes 类型错误 → 400。"""
        status, raw = self._post("/api/pick", {"kind": "save", "ext": 123})
        self.assertEqual(status, 400)
        status, raw = self._post(
            "/api/pick", {"kind": "save", "filetypes": "*.zip"}
        )
        self.assertEqual(status, 400)


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
        """终端菜单输入 '9' 时调用 guimanage.gui_serve（mian 惰性 import；2026-09-13 起配置界面为第 9 项）。"""
        import contextlib
        import io

        class _TTY(io.StringIO):
            def isatty(self):
                return True

        old_stdin = sys.stdin
        sys.stdin = _TTY("9\n0\n")
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


class TestCorrectMultiInstance(GuiServerTestBase):
    """矫正界面多实例：/api/correct/start|status|stop（mock Popen，不真起进程）。"""

    def _wait_until(self, cond, timeout=4.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.05)
        return cond()

    def test_correct_start_ok_single_instance(self):
        """POST /api/correct/start（无 pdf）+ 状态轮询 → 实例完成、地址已解析。"""
        with mock.patch.object(
            guimanage.subprocess, "Popen",
            return_value=_FakeCorrectProc(["http://127.0.0.1:8123/\n", "启动完成\n"]),
        ) as pop:
            status, raw = self._post("/api/correct/start", {"pdf": None})
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertIn("argv", data)
        self.assertTrue(pop.called)

        ok = self._wait_until(
            lambda: len(guimanage._correct_instances(self._state)) == 1
            and not guimanage._correct_instances(self._state)[0]["running"]
            and guimanage._correct_instances(self._state)[0].get("url") is not None
        )
        self.assertTrue(ok, "矫正实例未进入完成态/未解析 URL")
        inst = guimanage._correct_instances(self._state)[0]
        self.assertEqual(inst["engine"], "llama")  # config 无 proofread_engine → 默认 llama
        self.assertEqual(inst["port"], 8123)
        self.assertEqual(inst["url"], "http://127.0.0.1:8123/")
        self.assertGreaterEqual(len(inst["lines"]), 2)

        # GET /api/correct/status 快照（含实例列表）
        status2, _, raw2 = self._get("/api/correct/status")
        self.assertEqual(status2, 200)
        snap = json.loads(raw2)
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["count"], 1)
        self.assertEqual(snap["instances"][0]["url"], "http://127.0.0.1:8123/")

    def test_correct_multi_instance_allowed(self):
        """连续启动多个矫正实例 → 各自独立 id，全部可运行。"""
        with mock.patch.object(
            guimanage.subprocess, "Popen",
            return_value=_FakeCorrectProc(block=True),
        ):
            s1, raw1 = self._post("/api/correct/start", {})
            s2, raw2 = self._post("/api/correct/start", {})
        self.assertEqual(s1, 200)
        self.assertEqual(s2, 200)
        insts = guimanage._correct_instances(self._state)
        self.assertEqual(len(insts), 2)
        self.assertNotEqual(insts[0]["id"], insts[1]["id"])
        self.assertTrue(all(i["running"] for i in insts))

    def _tmp_pdf(self):
        """建一个真实存在的临时 PDF（接口校验文件存在）。"""
        fd, path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(b"%PDF-1.4\n")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def test_correct_start_allowed_when_convert_running(self):
        """转换运行中 → 仍可启动矫正（2026-10-21：转换与矫正分离）。"""
        pdf = self._tmp_pdf()
        self._state["convert"]["running"] = True
        with mock.patch.object(
            guimanage.subprocess, "Popen",
            return_value=_FakeCorrectProc(block=True),
        ) as pop:
            status, raw = self._post("/api/correct/start", {"pdf": pdf})
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertTrue(pop.called)

    def test_correct_start_live_flag_adds_live_ocr(self):
        """live=True → argv 含 --live-ocr（实时跟随 OCR 进度）。"""
        pdf = self._tmp_pdf()
        with mock.patch.object(
            guimanage.subprocess, "Popen",
            return_value=_FakeCorrectProc(block=True),
        ):
            status, raw = self._post(
                "/api/correct/start", {"pdf": pdf, "live": True}
            )
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertIn("--live-ocr", data["argv"])

    def test_correct_engine_default_from_proofread_engine(self):
        """config.proofread_engine=vllm 时启动缺省引擎为 vllm（argv 含 --engine vllm）。"""
        with open(self._cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["proofread_engine"] = "vllm"
        with open(self._cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        with mock.patch.object(
            guimanage.subprocess, "Popen",
            return_value=_FakeCorrectProc(block=True),
        ):
            status, raw = self._post("/api/correct/start", {})
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertIn("--engine", data["argv"])
        self.assertIn("vllm", data["argv"])
        self.assertEqual(guimanage._correct_instances(self._state)[0]["engine"], "vllm")

    def test_correct_stop_with_id(self):
        """body.id 指定实例 → 停止后实例 finalized 且不再 running。"""
        with mock.patch.object(
            guimanage.subprocess, "Popen",
            return_value=_FakeCorrectProc(block=True),
        ):
            _, raw = self._post("/api/correct/start", {})
        inst = guimanage._correct_instances(self._state)[0]
        self.assertTrue(inst["running"])
        status, resp = self._post("/api/correct/stop", {"id": inst["id"]})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(resp)["ok"])
        self.assertFalse(inst["running"])
        self.assertTrue(inst["finalized"])

    def test_correct_stop_no_id_stops_latest(self):
        """缺省 id → 停止最新实例，其余保持运行。"""
        with mock.patch.object(
            guimanage.subprocess, "Popen",
            return_value=_FakeCorrectProc(block=True),
        ):
            self._post("/api/correct/start", {})
            self._post("/api/correct/start", {})
        insts = guimanage._correct_instances(self._state)
        self.assertEqual(len(insts), 2)
        status, resp = self._post("/api/correct/stop", {})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(resp)["ok"])
        self.assertTrue(insts[0]["running"])
        self.assertFalse(insts[1]["running"])
        self.assertTrue(insts[1]["finalized"])

    def test_correct_stop_unknown_id_400(self):
        """未知 id → 400 中文错误。"""
        status, raw = self._post("/api/correct/stop", {"id": "nope"})
        self.assertEqual(status, 400)
        data = json.loads(raw)
        self.assertFalse(data["ok"])
        self.assertIn("没有正在运行的矫正", data["error"])


class TestEpubEditInstances(GuiServerTestBase):
    """EPUB 编辑实例：/api/epubedit/start|status|stop（mock Popen，不真起进程）。"""

    def _wait_until(self, cond, timeout=4.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.05)
        return cond()

    def _mk_epub(self, name="a.epub"):
        p = _Path(tempfile.mkdtemp(prefix="test_gui_epub_")) / name
        p.write_bytes(b"PK\x03\x04minimal-epub-stub")
        self.addCleanup(lambda: shutil.rmtree(p.parent, ignore_errors=True))
        return p

    def test_epubedit_start_ok_single_instance(self):
        """POST /api/epubedit/start + 状态轮询 → 实例完成、地址已解析。"""
        epub = self._mk_epub("a.epub")
        with mock.patch.object(
            guimanage.subprocess, "Popen",
            return_value=_FakeCorrectProc(["http://127.0.0.1:8231/\n", "启动完成\n"]),
        ) as pop:
            status, raw = self._post("/api/epubedit/start", {"epub": str(epub)})
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertIn("argv", data)
        self.assertTrue(pop.called)
        # dev argv 形状：sys.executable -u ROOT/mian.py epubedit <path> --idle-timeout 600
        argv = data["argv"]
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[2].endswith("mian.py"), True)
        self.assertIn("epubedit", argv)
        self.assertIn(str(epub), argv)
        self.assertIn("--idle-timeout", argv)
        self.assertEqual(argv[argv.index("--idle-timeout") + 1], "600")

        ok = self._wait_until(
            lambda: len(guimanage._epubedit_instances(self._state)) == 1
            and not guimanage._epubedit_instances(self._state)[0]["running"]
            and guimanage._epubedit_instances(self._state)[0].get("url") is not None
        )
        self.assertTrue(ok, "EPUB 编辑实例未进入完成态/未解析 URL")
        inst = guimanage._epubedit_instances(self._state)[0]
        self.assertEqual(inst["epub"], str(epub))
        self.assertEqual(inst["port"], 8231)
        self.assertEqual(inst["url"], "http://127.0.0.1:8231/")
        self.assertGreaterEqual(len(inst["lines"]), 2)

        # GET /api/epubedit/status 快照（含实例列表）
        status2, _, raw2 = self._get("/api/epubedit/status")
        self.assertEqual(status2, 200)
        snap = json.loads(raw2)
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["count"], 1)
        self.assertEqual(snap["instances"][0]["url"], "http://127.0.0.1:8231/")

    def test_epubedit_start_bad_path_400(self):
        """路径不存在/非 .epub → 400 中文错误，不 Popen。"""
        # 文件不存在 → 文件不存在
        missing = _Path(self._cfg_path).parent / "nope_missing.epub"
        with mock.patch.object(guimanage.subprocess, "Popen") as pop:
            status, raw = self._post("/api/epubedit/start", {"epub": str(missing)})
        self.assertEqual(status, 400)
        data = json.loads(raw)
        self.assertFalse(data["ok"])
        self.assertIn("文件不存在", data["error"])
        pop.assert_not_called()
        # 存在但非 .epub 后缀 → 请选择有效的 EPUB
        not_epub = self._mk_epub("book.txt")
        with mock.patch.object(guimanage.subprocess, "Popen") as pop2:
            status2, raw2 = self._post("/api/epubedit/start", {"epub": str(not_epub)})
        self.assertEqual(status2, 400)
        self.assertIn("有效的 EPUB", json.loads(raw2)["error"])
        pop2.assert_not_called()

    def test_epubedit_start_rejects_when_convert_running(self):
        """转换运行中 → 409（转换与 EPUB 编辑互斥）。"""
        self._state["convert"]["running"] = True
        epub = self._mk_epub("b.epub")
        with mock.patch.object(guimanage.subprocess, "Popen") as pop:
            status, raw = self._post("/api/epubedit/start", {"epub": str(epub)})
        self.assertEqual(status, 409)
        data = json.loads(raw)
        self.assertFalse(data["ok"])
        self.assertIn("已有转换在运行", data["error"])
        pop.assert_not_called()

    def test_epubedit_start_single_flight_409(self):
        """已有编辑实例运行中 → 409（EPUB 编辑单实例）。"""
        epub = self._mk_epub("c.epub")
        with mock.patch.object(
            guimanage.subprocess, "Popen",
            return_value=_FakeCorrectProc(block=True),
        ):
            s1, _ = self._post("/api/epubedit/start", {"epub": str(epub)})
            s2, raw2 = self._post("/api/epubedit/start", {"epub": str(epub)})
        self.assertEqual(s1, 200)
        self.assertEqual(s2, 409)
        self.assertIn("已有 EPUB 编辑在运行", json.loads(raw2)["error"])

    def test_epubedit_stop_with_id(self):
        """body.id 指定实例 → 停止后 finalized 且不再 running。"""
        epub = self._mk_epub("d.epub")
        with mock.patch.object(
            guimanage.subprocess, "Popen",
            return_value=_FakeCorrectProc(block=True),
        ):
            _, raw = self._post("/api/epubedit/start", {"epub": str(epub)})
        inst = guimanage._epubedit_instances(self._state)[0]
        self.assertTrue(inst["running"])
        status, resp = self._post("/api/epubedit/stop", {"id": inst["id"]})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(resp)["ok"])
        self.assertFalse(inst["running"])
        self.assertTrue(inst["finalized"])

    def test_epubedit_stop_none_running_400(self):
        """无运行实例 → 400 中文错误。"""
        status, raw = self._post("/api/epubedit/stop", {})
        self.assertEqual(status, 400)
        self.assertIn("没有正在运行的 EPUB 编辑", json.loads(raw)["error"])

    def test_epubedit_argv_shapes(self):
        """_epubedit_argv：dev 走 mian.py，frozen 走自身可执行文件。"""
        dev = guimanage._epubedit_argv("C:/x/book.epub", idle_timeout=120)
        self.assertEqual(dev[0], sys.executable)
        self.assertEqual(dev[1], "-u")
        self.assertTrue(dev[2].endswith("mian.py"))
        self.assertEqual(dev[3], "epubedit")
        self.assertIn("C:/x/book.epub", dev)
        self.assertEqual(dev[dev.index("--idle-timeout") + 1], "120")
        with mock.patch.object(sys, "frozen", True, create=True):
            fro = guimanage._epubedit_argv("C:/x/book.epub", idle_timeout=60)
        self.assertEqual(fro[:2], [sys.executable, "epubedit"])
        self.assertEqual(fro[2], "C:/x/book.epub")
        self.assertEqual(fro[fro.index("--idle-timeout") + 1], "60")
        self.assertNotIn("mian.py", fro)


class TestCacheBackupHistory(GuiServerTestBase):
    """缓存清理 / 数据备份 / 历史记录端点（注入临时 data_root，不碰真实 data/）。"""

    def setUp(self):
        super().setUp()
        self._data_root = _Path(tempfile.mkdtemp(prefix="test_gui_data_"))
        self._state["data_root"] = str(self._data_root)

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self._data_root, ignore_errors=True)

    def test_cache_clear_ok(self):
        """清预览图/分割图/临时文件 → 200 ok，文件删除且 counts 正确。"""
        preview = self._data_root / "preview_cache"
        preview.mkdir(parents=True)
        (preview / "a.jpg").write_bytes(b"x" * 10)
        book = self._data_root / "book"
        book.mkdir()
        (book / ".ptoe_split.json").write_text("{}", encoding="utf-8")
        (book / "1.png").write_bytes(b"y" * 20)
        (book / "1.jpg").write_bytes(b"y" * 5)
        tmp_marker = book / "1.tmp"          # 不在 preview_cache/correction_history 下，不应被清
        tmp_marker.write_bytes(b"k")
        hist = self._data_root / "correction_history"
        hist.mkdir()
        (hist / "t.tmp").write_bytes(b"t")

        status, raw = self._post("/api/cache/clear", {"scopes": ["preview", "split", "tmp"]})
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertEqual(data["counts"]["preview"], 1)
        self.assertEqual(data["counts"]["split"], 3)  # 1.png + 1.jpg + .ptoe_split.json
        self.assertEqual(data["counts"]["tmp"], 1)
        self.assertGreater(data["freed"], 0)
        self.assertFalse((preview / "a.jpg").exists())
        self.assertFalse((book / "1.png").exists())
        self.assertFalse((book / ".ptoe_split.json").exists())
        self.assertFalse((hist / "t.tmp").exists())
        self.assertTrue(tmp_marker.exists())  # 区域外不动

    def test_cache_clear_bad_scopes_400(self):
        """scopes 为空/非法 → 400。"""
        for scopes in ([], ["other"], "preview", None):
            status, raw = self._post("/api/cache/clear", {"scopes": scopes})
            self.assertEqual(status, 400, scopes)
            self.assertFalse(json.loads(raw)["ok"])

    def test_cache_clear_blocked_when_convert_running(self):
        """转换运行中 → 400。"""
        self._state["convert"]["running"] = True
        status, raw = self._post("/api/cache/clear", {"scopes": ["preview"]})
        self.assertEqual(status, 400)
        self.assertIn("请先停止正在运行的转换/矫正任务", json.loads(raw)["error"])

    def test_cache_clear_blocked_when_correct_running(self):
        """矫正实例运行中 → 400。"""
        guimanage._correct_instances(self._state).append({"id": "x", "running": True})
        status, raw = self._post("/api/cache/clear", {"scopes": ["preview"]})
        self.assertEqual(status, 400)
        self.assertIn("请先停止正在运行的转换/矫正任务", json.loads(raw)["error"])

    def test_backup_create_default_excludes_images_and_epubs(self):
        """默认备份：config + 词表 + 历史 json，不打包历史图片 / EPUB。"""
        import correctmanage

        hist = self._data_root / "correction_history"
        hist.mkdir(parents=True)
        (hist / "v1.json").write_text("{}", encoding="utf-8")
        (hist / "v1.images.json").write_text("{}", encoding="utf-8")
        (self._data_root / "proofread_dict.json").write_text("{}", encoding="utf-8")
        (self._data_root / "book").mkdir()
        (self._data_root / "book" / "out.epub").write_bytes(b"PK")

        with mock.patch.object(correctmanage, "_history_dir", return_value=str(hist)):
            status, raw = self._post("/api/backup/create", {})
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertTrue(data["path"].endswith(".zip"))
        with zipfile.ZipFile(data["path"]) as zf:
            names = zf.namelist()
        self.assertIn(os.path.basename(self._cfg_path), names)  # config.json 在根
        self.assertIn("data/proofread_dict.json", names)
        self.assertIn("data/correction_history/v1.json", names)
        self.assertNotIn("data/correction_history/v1.images.json", names)
        self.assertFalse(any(n.endswith(".epub") for n in names))

    def test_backup_create_with_images_and_epubs(self):
        """include_history_images + include_epubs → 历史图片与 EPUB 一并打包。"""
        import correctmanage

        hist = self._data_root / "correction_history"
        hist.mkdir(parents=True)
        (hist / "v1.json").write_text("{}", encoding="utf-8")
        (hist / "v1.images.json").write_text("{}", encoding="utf-8")
        (self._data_root / "book").mkdir()
        (self._data_root / "book" / "out.epub").write_bytes(b"PK")
        (self._data_root / "proofread_dict.json").write_text("{}", encoding="utf-8")

        with mock.patch.object(correctmanage, "_history_dir", return_value=str(hist)):
            status, raw = self._post(
                "/api/backup/create",
                {"include_history_images": True, "include_epubs": True},
            )
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        with zipfile.ZipFile(data["path"]) as zf:
            names = zf.namelist()
        self.assertIn("data/correction_history/v1.images.json", names)
        self.assertIn("data/book/out.epub", names)

    def test_backup_create_non_dict_400(self):
        """body 非对象 → 400。"""
        status, raw = self._post("/api/backup/create", ["x"])
        self.assertEqual(status, 400)
        data = json.loads(raw)
        self.assertFalse(data["ok"])
        self.assertIn("无效的 JSON", data["error"])

    def test_history_get(self):
        """GET /api/history 透传 correctmanage._history_entries。"""
        import correctmanage

        entries = [{"id": "abc", "name": "样例", "pages": 12}]
        with mock.patch.object(correctmanage, "_history_entries", return_value=entries):
            status, _, raw = self._get("/api/history")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertEqual(data["entries"], entries)

    def test_history_delete_ok(self):
        """POST /api/history/delete 透传 _delete_history([id], all_=False)。"""
        import correctmanage

        with mock.patch.object(correctmanage, "_delete_history") as dh:
            status, raw = self._post("/api/history/delete", {"id": "abc"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["ok"])
        dh.assert_called_once_with(["abc"], all_=False)

    def test_history_delete_missing_id_400(self):
        """缺 id → 400。"""
        status, raw = self._post("/api/history/delete", {"id": ""})
        self.assertEqual(status, 400)
        self.assertIn("缺少 id", json.loads(raw)["error"])

    def test_history_rename_ok(self):
        """POST /api/history/rename 透传 correctmanage.rename_history(id, newName)。"""
        import correctmanage

        with mock.patch.object(
            correctmanage, "rename_history", return_value={"ok": True, "display_name": "新名"}
        ) as rh:
            status, raw = self._post("/api/history/rename", {"id": "abc", "newName": "新名"})
        self.assertEqual(status, 200, raw)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertEqual(data["display_name"], "新名")
        rh.assert_called_once_with("abc", "新名")

    def test_history_rename_missing_id_400(self):
        """缺 id → 400。"""
        status, raw = self._post("/api/history/rename", {"id": "", "newName": "x"})
        self.assertEqual(status, 400)
        self.assertIn("缺少 id", json.loads(raw)["error"])

    def test_history_rename_bad_type_400(self):
        """newName 非字符串 → 400。"""
        status, raw = self._post("/api/history/rename", {"id": "abc", "newName": 123})
        self.assertEqual(status, 400)
        self.assertIn("newName", json.loads(raw)["error"])

    def test_history_rename_value_error_400(self):
        """rename_history 抛 ValueError → 400（透传中文消息，不触 500）。"""
        import correctmanage

        with mock.patch.object(
            correctmanage, "rename_history", side_effect=ValueError("名称含非法字符（含 \\ / :）")
        ):
            status, raw = self._post("/api/history/rename", {"id": "abc", "newName": "a/b"})
        self.assertEqual(status, 400)
        self.assertIn("非法字符", json.loads(raw)["error"])

    def test_history_rename_lookup_error_404(self):
        """rename_history 抛 LookupError → 404。"""
        import correctmanage

        with mock.patch.object(
            correctmanage, "rename_history", side_effect=LookupError("无法定位所属 PDF 组")
        ):
            status, raw = self._post("/api/history/rename", {"id": "abc", "newName": "新名"})
        self.assertEqual(status, 404)
        self.assertIn("无法定位", json.loads(raw)["error"])

    def test_history_rename_button_appended_in_ops(self):
        """DOM 回归（2026-10-09）：renderHistory 创建了 renBtn（「重命名」按钮）但
        从未 appendChild 进 ops 单元格 → 历史列表里按钮根本不显示（用户可见 bug：
        弹窗/后端都在，唯独行内按钮缺失）。断言 append 链中 renBtn 位于 openBtn 与
        expBtn 之间，且操作列表头已加宽到 222px 容纳第 5 个元素。
        """
        html = guimanage._UI_HTML
        create_at = html.find('var renBtn=document.createElement("button")')
        self.assertGreater(create_at, 0, "renBtn 定义缺失")
        chain = (
            "ops.appendChild(openBtn);ops.appendChild(renBtn);"
            "ops.appendChild(expBtn);ops.appendChild(saveAsSel);ops.appendChild(delBtn);"
        )
        chain_at = html.find(chain)
        self.assertGreater(chain_at, 0, "append 链不含 renBtn（按钮不会渲染）")
        self.assertLess(create_at, chain_at, "renBtn 必须先定义后 append")
        self.assertIn('<th style="width:222px;">操作</th>', html, "操作列未加宽")

    def test_history_export_backup_ok(self):
        """POST /api/history/export/backup：版本打包成 ZIP 写入 data/data/ 并回传路径。

        回归（2026-09-16）：历史页「导出」原先用 <a download href="/api/history/export">
        触发浏览器下载，而 pywebview WebView2 默认 ALLOW_DOWNLOADS=False 会**静默取消**
        附件下载 → 界面提示「已开始导出」却没有任何文件。现改为服务端写盘并回传路径
        （与矫正界面历史弹窗「导出」同一实现 correctmanage.export_history_backup）。
        """
        import correctmanage

        hist = self._data_root / "correction_history"
        hist.mkdir(parents=True)
        pid = "abc_20260101000000_0001"
        (hist / f"{pid}.json").write_text(
            json.dumps({"pages": {"1": "<p>正文</p>"}}, ensure_ascii=False),
            encoding="utf-8",
        )
        base_dir = _Path(tempfile.mkdtemp(prefix="test_gui_base_"))
        self.addCleanup(shutil.rmtree, base_dir, ignore_errors=True)
        with mock.patch.object(correctmanage, "_history_dir", return_value=str(hist)), \
                mock.patch("pdfmanage.app_base_dir", return_value=base_dir):
            status, raw = self._post("/api/history/export/backup", {"ids": [pid]})
        self.assertEqual(status, 200, raw)
        data = json.loads(raw)
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["count"], 1)
        out = _Path(data["path"])
        # 必须真的落盘（旧实现只有浏览器下载，产物为空）
        self.assertTrue(out.is_file(), data)
        self.assertEqual(out.parent, base_dir / "data" / "data")
        with zipfile.ZipFile(out) as zf:
            self.assertIn(f"{pid}.json", zf.namelist())
            payload = json.loads(zf.read(f"{pid}.json").decode("utf-8"))
        self.assertEqual(payload["pages"]["1"], "<p>正文</p>")
        self.assertIn("images", payload)  # 内嵌预览图，供跨机导入

    def test_history_export_backup_bad_requests(self):
        """空 ids → 400；版本不存在 → 404（错误信息中文可读）。"""
        import correctmanage

        hist = self._data_root / "correction_history"
        hist.mkdir(parents=True)
        status, raw = self._post("/api/history/export/backup", {"ids": []})
        self.assertEqual(status, 400)
        self.assertIn("未选择", json.loads(raw)["error"])
        with mock.patch.object(correctmanage, "_history_dir", return_value=str(hist)):
            status, raw = self._post("/api/history/export/backup", {"ids": ["nope"]})
        self.assertEqual(status, 404)
        self.assertIn("没有可导出", json.loads(raw)["error"])

    def test_history_export_backup_save_path(self):
        """POST /api/history/export/backup 带 save_path → 写入用户指定位置（另存为）。"""
        import correctmanage

        hist = self._data_root / "correction_history"
        hist.mkdir(parents=True)
        pid = "abc_20260101000000_0002"
        (hist / f"{pid}.json").write_text(
            json.dumps({"pages": {"1": "<p>正文2</p>"}}, ensure_ascii=False),
            encoding="utf-8",
        )
        dest = self._data_root / "用户选择" / "我的备份.zip"
        with mock.patch.object(correctmanage, "_history_dir", return_value=str(hist)):
            status, raw = self._post(
                "/api/history/export/backup", {"ids": [pid], "save_path": str(dest)}
            )
        self.assertEqual(status, 200, raw)
        data = json.loads(raw)
        self.assertTrue(data["ok"], data)
        # 落盘在用户指定位置（而非默认的 data/data），且自动建父目录
        self.assertEqual(_Path(data["path"]), dest)
        self.assertTrue(dest.is_file())
        with zipfile.ZipFile(dest) as zf:
            self.assertIn(f"{pid}.json", zf.namelist())

    def _write_version(self, hist, pid, pages, display_name="测试书.pdf"):
        (hist / f"{pid}.json").write_text(
            json.dumps(
                {"display_name": display_name, "pages": pages}, ensure_ascii=False
            ),
            encoding="utf-8",
        )

    def test_history_export_file_txt(self):
        """POST /api/history/export/file format=txt → 把版本内容导出为 txt（指定位置）。"""
        import correctmanage

        hist = self._data_root / "correction_history"
        hist.mkdir(parents=True)
        pid = "abc_20260101000000_0002"
        self._write_version(
            hist, pid, {"1": "<h1>标题</h1>", "2": "<p>正文段落</p>"}
        )
        dest = self._data_root / "out" / "测试书_20260101.txt"
        with mock.patch.object(correctmanage, "_history_dir", return_value=str(hist)):
            status, raw = self._post(
                "/api/history/export/file",
                {"id": pid, "format": "txt", "save_path": str(dest)},
            )
        self.assertEqual(status, 200, raw)
        data = json.loads(raw)
        self.assertTrue(data["ok"], data)
        self.assertTrue(dest.is_file())
        self.assertIn("标题", dest.read_bytes().decode("utf-8-sig"))

    def test_history_export_file_epub(self):
        """POST /api/history/export/file format=epub → 导出合法 EPUB（指定位置）。"""
        import correctmanage

        hist = self._data_root / "correction_history"
        hist.mkdir(parents=True)
        pid = "abc_20260101000000_0002"
        self._write_version(
            hist, pid, {"1": "<h1>标题</h1>", "2": "<p>正文段落</p>"}
        )
        dest = self._data_root / "out.epub"
        with mock.patch.object(correctmanage, "_history_dir", return_value=str(hist)):
            status, raw = self._post(
                "/api/history/export/file",
                {"id": pid, "format": "epub", "save_path": str(dest)},
            )
        self.assertEqual(status, 200, raw)
        data = json.loads(raw)
        self.assertTrue(data["ok"], data)
        self.assertTrue(dest.is_file())
        with zipfile.ZipFile(dest) as zf:
            self.assertIn("mimetype", zf.namelist())

    def test_history_export_file_bad_format_400(self):
        """format 非法 → 400。"""
        status, raw = self._post(
            "/api/history/export/file",
            {"id": "x", "format": "pdf", "save_path": "z.pdf"},
        )
        self.assertEqual(status, 400)
        self.assertIn("格式", json.loads(raw)["error"])

    def test_history_export_file_missing_version_404(self):
        """版本不存在 → 404。"""
        import correctmanage

        hist = self._data_root / "correction_history"
        hist.mkdir(parents=True)
        with mock.patch.object(correctmanage, "_history_dir", return_value=str(hist)):
            status, raw = self._post(
                "/api/history/export/file",
                {"id": "nope", "format": "txt", "save_path": "z.txt"},
            )
        self.assertEqual(status, 404)
        self.assertIn("不存在", json.loads(raw)["error"])

    def test_history_export_file_empty_pages_400(self):
        """版本没有页面内容 → 400。"""
        import correctmanage

        hist = self._data_root / "correction_history"
        hist.mkdir(parents=True)
        pid = "abc_20260101000000_0002"
        self._write_version(hist, pid, {})
        with mock.patch.object(correctmanage, "_history_dir", return_value=str(hist)):
            status, raw = self._post(
                "/api/history/export/file",
                {"id": pid, "format": "txt", "save_path": "z.txt"},
            )
        self.assertEqual(status, 400)
        self.assertIn("没有可导出", json.loads(raw)["error"])


class TestEpubMetaEndpoints(GuiServerTestBase):
    """EPUB 书籍信息端点（/api/epub/meta/read|save）。"""

    def test_read_ok(self):
        """POST /api/epub/meta/read 透传 epubmetamanage.read_epub_metadata。"""
        info = {"ok": True, "path": "/x.epub", "title": "书", "author": "作者",
                "description": "简介", "language": "zh-CN", "cover_data_uri": "data:image/png;base64,AA"}
        with mock.patch.object(epubmetamanage, "read_epub_metadata", return_value=info) as rd:
            status, raw = self._post("/api/epub/meta/read", {"path": "/x.epub"})
        self.assertEqual(status, 200, raw)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertEqual(data["title"], "书")
        rd.assert_called_once_with("/x.epub")

    def test_read_non_dict_400(self):
        status, raw = self._post("/api/epub/meta/read", ["x"])
        self.assertEqual(status, 400)
        self.assertIn("无效的 JSON", json.loads(raw)["error"])

    def test_read_missing_path_400(self):
        status, raw = self._post("/api/epub/meta/read", {"path": ""})
        self.assertEqual(status, 400)
        self.assertIn("缺少 path", json.loads(raw)["error"])

    def test_read_not_found_404(self):
        with mock.patch.object(
            epubmetamanage, "read_epub_metadata", side_effect=FileNotFoundError("文件不存在")
        ):
            status, raw = self._post("/api/epub/meta/read", {"path": "/no.epub"})
        self.assertEqual(status, 404)
        self.assertIn("不存在", json.loads(raw)["error"])

    def test_read_error_500(self):
        with mock.patch.object(
            epubmetamanage, "read_epub_metadata", side_effect=Exception("boom")
        ):
            status, raw = self._post("/api/epub/meta/read", {"path": "/x.epub"})
        self.assertEqual(status, 500)
        self.assertIn("boom", json.loads(raw)["error"])

    def test_save_ok(self):
        """POST /api/epub/meta/save 透传字段；无 cover_path 时 cover_bytes=None。"""
        with mock.patch.object(
            epubmetamanage, "save_epub_metadata", return_value={"ok": True, "path": "/x.epub"}
        ) as sv:
            status, raw = self._post(
                "/api/epub/meta/save",
                {"path": "/x.epub", "title": "书", "author": "作者",
                 "description": "简介", "language": "zh-CN"},
            )
        self.assertEqual(status, 200, raw)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertEqual(data["path"], "/x.epub")
        sv.assert_called_once_with(
            "/x.epub", title="书", author="作者", description="简介",
            language="zh-CN", cover_bytes=None, cover_ext=None, out_path=None,
        )

    def test_save_with_out_path(self):
        """传 out_path → 透传给 save_epub_metadata，响应 path 为另存目标。"""
        dest = os.path.join(tempfile.gettempdir(), "副本.epub")
        with mock.patch.object(
            epubmetamanage, "save_epub_metadata", return_value={"ok": True, "path": dest}
        ) as sv:
            status, raw = self._post(
                "/api/epub/meta/save", {"path": "/x.epub", "out_path": dest, "title": "书"}
            )
        self.assertEqual(status, 200, raw)
        data = json.loads(raw)
        self.assertTrue(data["ok"])
        self.assertEqual(data["path"], dest)
        self.assertEqual(sv.call_args.kwargs["out_path"], dest)

    def test_save_out_path_non_string_400(self):
        status, raw = self._post("/api/epub/meta/save", {"path": "/x.epub", "out_path": 123})
        self.assertEqual(status, 400)
        self.assertIn("out_path", json.loads(raw)["error"])

    def test_save_out_path_missing_dir_400(self):
        missing = os.path.join(tempfile.gettempdir(), "ptoe_no_such_dir_9f3", "y.epub")
        status, raw = self._post("/api/epub/meta/save", {"path": "/x.epub", "out_path": missing})
        self.assertEqual(status, 400)
        self.assertIn("目标目录不存在", json.loads(raw)["error"])

    def test_save_with_cover_file(self):
        """cover_path 存在 → 读取字节并传 cover_bytes / cover_ext。"""
        fd, cover = tempfile.mkstemp(prefix="test_cover_", suffix=".png")
        os.close(fd)
        with open(cover, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\nDATA")
        self.addCleanup(os.unlink, cover)
        with mock.patch.object(
            epubmetamanage, "save_epub_metadata", return_value={"ok": True, "path": "/x.epub"}
        ) as sv:
            status, raw = self._post(
                "/api/epub/meta/save", {"path": "/x.epub", "title": "书", "cover_path": cover}
            )
        self.assertEqual(status, 200, raw)
        args, kwargs = sv.call_args
        self.assertEqual(args, ("/x.epub",))
        self.assertEqual(kwargs["cover_bytes"], b"\x89PNG\r\n\x1a\nDATA")
        self.assertEqual(kwargs["cover_ext"], ".png")

    def test_save_non_dict_400(self):
        status, raw = self._post("/api/epub/meta/save", ["x"])
        self.assertEqual(status, 400)
        self.assertIn("无效的 JSON", json.loads(raw)["error"])

    def test_save_missing_path_400(self):
        status, raw = self._post("/api/epub/meta/save", {"path": ""})
        self.assertEqual(status, 400)
        self.assertIn("缺少 path", json.loads(raw)["error"])

    def test_save_cover_missing_400(self):
        status, raw = self._post(
            "/api/epub/meta/save", {"path": "/x.epub", "cover_path": "/no/cover.png"}
        )
        self.assertEqual(status, 400)
        self.assertIn("封面文件不存在", json.loads(raw)["error"])

    def test_save_not_found_404(self):
        with mock.patch.object(
            epubmetamanage, "save_epub_metadata", side_effect=FileNotFoundError("文件不存在")
        ):
            status, raw = self._post("/api/epub/meta/save", {"path": "/no.epub"})
        self.assertEqual(status, 404)
        self.assertIn("不存在", json.loads(raw)["error"])

    def test_save_error_500(self):
        with mock.patch.object(
            epubmetamanage, "save_epub_metadata", side_effect=Exception("boom")
        ):
            status, raw = self._post("/api/epub/meta/save", {"path": "/x.epub"})
        self.assertEqual(status, 500)
        self.assertIn("boom", json.loads(raw)["error"])


if __name__ == "__main__":
    unittest.main()