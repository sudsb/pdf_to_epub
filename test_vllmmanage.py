"""test_vllmmanage.py — vLLM-Omni 推理引擎适配层单元测试。

覆盖：
- vllmmanage._probe_server：/health 与 /v1/models 两段探测的四种结果
  （none / match / mismatch），含相对模型名匹配完整路径 id；
- vllmmanage.runserver：启动模式（拼 `vllm serve` 命令行 + 就绪轮询）、
  复用已运行服务、连接模式（vllm_server 为空时不拉起进程）；
- vllmmanage._request_image_new：多模态载荷形状（text/image_url 块、
  modalities=["text"] 跳过音频、无 stop 键、data URI 前缀）；
- llamamanage 的引擎分发：engine=vllm 时各公开函数转调 vllmmanage，
  engine=llama 时不触碰 vllmmanage。

所有网络/子进程调用均以 mock 替身，不依赖真实 vLLM 服务与配置文件。
"""

import unittest
from unittest import mock

import llamamanage as llm
import vllmmanage as vm


# 假配置：_vll_args() 的 4 元组 (vllm_server, vllm_server_args, models_dir, model_choices)
FAKE_ARGS = (
    "C:/vllm/vllm.exe",
    {"host": "127.0.0.1", "port": 8000},
    "C:/models",
    {"HY": {"name": "HY", "mmproj": ""}},
)


def _resp(status=200, payload=None):
    """构造一个假的 requests.Response（仅 status_code / json()）。"""
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    return r


class _EngineResetMixin:
    """每个用例前后清理引擎覆盖与模块级进程句柄，避免跨用例污染。"""

    def setUp(self):
        llm.set_engine(None)
        vm._server_process = None
        llm._server_process = None

    def tearDown(self):
        # 必须清引擎覆盖（同时清 _ENGINE_CACHE），否则污染其他测试
        llm.set_engine(None)
        vm._server_process = None
        llm._server_process = None


class TestProbeServer(_EngineResetMixin, unittest.TestCase):
    """vllmmanage._probe_server：/health 存活 + /v1/models 就绪两段探测。"""

    def test_health_not_ok_returns_none(self):
        with mock.patch.object(vm, "_vll_args", return_value=FAKE_ARGS), \
             mock.patch.object(vm, "_SESSION") as sess:
            sess.get.return_value = _resp(503)
            self.assertEqual(vm._probe_server("HY"), "none")

    def test_relative_name_matches_full_path_id(self):
        # config 存相对路径，vLLM /v1/models 报告完整路径 → 应判 match
        payload = {"data": [{"id": "E:/model/qwen3.5/Qwen3.5-0.8B-Q8_0.gguf"}]}
        with mock.patch.object(vm, "_vll_args", return_value=FAKE_ARGS), \
             mock.patch.object(vm, "_SESSION") as sess:
            sess.get.side_effect = [_resp(200), _resp(200, payload)]
            self.assertEqual(
                vm._probe_server("qwen3.5/Qwen3.5-0.8B-Q8_0.gguf"), "match"
            )

    def test_different_model_is_mismatch(self):
        payload = {"data": [{"id": "E:/model/qwen3.5/Qwen3.5-0.8B-Q8_0.gguf"}]}
        with mock.patch.object(vm, "_vll_args", return_value=FAKE_ARGS), \
             mock.patch.object(vm, "_SESSION") as sess:
            sess.get.side_effect = [_resp(200), _resp(200, payload)]
            self.assertEqual(vm._probe_server("dots/dots.ocr.IQ4_XS.gguf"), "mismatch")

    def test_models_endpoint_loading_is_mismatch(self):
        # /health 200 但 /v1/models 503（模型仍在加载）→ mismatch
        with mock.patch.object(vm, "_vll_args", return_value=FAKE_ARGS), \
             mock.patch.object(vm, "_SESSION") as sess:
            sess.get.side_effect = [_resp(200), _resp(503)]
            self.assertEqual(vm._probe_server("HY"), "mismatch")


class TestRunserver(_EngineResetMixin, unittest.TestCase):
    """vllmmanage.runserver：启动 / 复用 / 连接模式。"""

    def test_launch_builds_command_and_waits_ready(self):
        args = ("C:/fake/vllm-server.exe", {"host": "127.0.0.1", "port": 8000},
                "C:/models", {"HY": {"name": "HY", "mmproj": ""}})
        proc = mock.Mock()
        proc.poll.return_value = None  # 进程存活
        ready = {"data": [{"id": "C:/models/HY"}]}

        with mock.patch.object(vm, "_vll_args", return_value=args), \
             mock.patch.object(vm, "_probe_server", return_value="none"), \
             mock.patch.object(vm.subprocess, "Popen", return_value=proc) as popen, \
             mock.patch.object(vm.time, "sleep"), \
             mock.patch.object(vm._SESSION, "get",
                               side_effect=[_resp(503), _resp(200, ready)]):
            ok = vm.runserver("HY")

        self.assertTrue(ok)
        self.assertIs(vm._server_process, proc)
        self.assertEqual(popen.call_count, 1)
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd[0], "C:/fake/vllm-server.exe")
        self.assertIn("serve", cmd)
        self.assertIn("HY", cmd)  # 模型路径/HF id
        self.assertIn("--port", cmd)
        self.assertIn("8000", cmd)

    def test_reuse_running_server_skips_popen(self):
        with mock.patch.object(vm, "_vll_args", return_value=FAKE_ARGS), \
             mock.patch.object(vm, "_probe_server", return_value="match"), \
             mock.patch.object(vm.subprocess, "Popen") as popen:
            ok = vm.runserver("HY")

        self.assertTrue(ok)
        self.assertEqual(popen.call_count, 0, "已在运行的同模型服务应直接复用")

    def test_connect_only_mode_without_executable(self):
        # vllm_server 为空 = 连接模式：不拉起进程，探测不到则失败返回
        args = ("", {"host": "127.0.0.1", "port": 8000},
                "C:/models", {"HY": {"name": "HY", "mmproj": ""}})
        with mock.patch.object(vm, "_vll_args", return_value=args), \
             mock.patch.object(vm, "_probe_server", return_value="none"), \
             mock.patch.object(vm.subprocess, "Popen") as popen:
            ok = vm.runserver("HY")

        self.assertFalse(ok)
        self.assertEqual(popen.call_count, 0, "连接模式不应启动本地进程")


class _FakeImg:
    """带 get_base64() 的假图片对象（鸭子类型，同 pdfmanage.ImageItem）。"""

    def __init__(self, b64="QUJD"):
        self._b64 = b64

    def get_base64(self):
        return self._b64


class TestRequestImage(_EngineResetMixin, unittest.TestCase):
    """vllmmanage._request_image_new：多模态请求体形状。"""

    def test_multimodal_payload_shape(self):
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}

        with mock.patch.object(vm, "_vll_args", return_value=FAKE_ARGS), \
             mock.patch.object(vm._SESSION, "post", return_value=resp) as post:
            res = vm._request_image_new("prompt", _FakeImg())

        self.assertIsNone(res["error"])
        self.assertEqual(res["result"], "ok")
        self.assertEqual(post.call_count, 1)

        url_args, kwargs = post.call_args
        self.assertIn("/v1/chat/completions", url_args[0])
        payload = kwargs["json"]

        content = payload["messages"][0]["content"]
        self.assertIsInstance(content, list)
        types = [blk.get("type") for blk in content]
        self.assertIn("text", types)
        self.assertIn("image_url", types)

        # vLLM-Omni 默认输出文本+音频，需显式跳过音频生成阶段
        self.assertEqual(payload["modalities"], ["text"])
        # 多模态 OCR 不能带 stop（会截断多段落输出）
        self.assertNotIn("stop", payload)

        img_blk = next(b for b in content if b.get("type") == "image_url")
        self.assertTrue(img_blk["image_url"]["url"].startswith("data:image/png;base64,"))


class TestDispatch(_EngineResetMixin, unittest.TestCase):
    """llamamanage 的引擎分发：engine=vllm → 转调 vllmmanage。"""

    def test_runserver_dispatches_to_vllm(self):
        llm.set_engine("vllm")
        with mock.patch.object(vm, "runserver", return_value=True) as m:
            ok = llm.runserver("HY", with_mmproj=True)

        self.assertTrue(ok)
        self.assertEqual(m.call_count, 1)
        self.assertEqual(m.call_args, mock.call("HY", with_mmproj=True, parallel=None))

    def test_runserver_llama_engine_does_not_touch_vllm(self):
        llm.set_engine("llama")
        cfg = ("exe", "mdir", {"HY": {"name": "HY.gguf", "mmproj": ""}}, "HY")
        with mock.patch.object(vm, "runserver") as m, \
             mock.patch.object(llm, "_reload_config", return_value=cfg), \
             mock.patch.object(llm.os.path, "exists", return_value=True), \
             mock.patch.object(llm, "_probe_server", return_value="match"):
            ok = llm.runserver("HY", with_mmproj=True)

        self.assertTrue(ok, "同模型已运行时 llama 路径应复用返回 True")
        self.assertEqual(m.call_count, 0, "engine=llama 不应转调 vllmmanage")

    def test_probe_server_dispatches_to_vllm(self):
        llm.set_engine("vllm")
        with mock.patch.object(vm, "_probe_server", return_value="match") as m:
            self.assertEqual(llm._probe_server("HY"), "match")
        self.assertEqual(m.call_args, mock.call("HY"))

    def test_stopserver_dispatches_to_vllm(self):
        llm.set_engine("vllm")
        with mock.patch.object(vm, "stopserver", return_value=True) as m:
            self.assertTrue(llm.stopserver())
        self.assertEqual(m.call_count, 1)

    def test_batch_infer_dispatches_to_vllm(self):
        llm.set_engine("vllm")
        images = ["/tmp/a.png", "/tmp/b.png"]
        out = [{"img": i, "result": "ok", "error": None} for i in images]
        with mock.patch.object(vm, "batch_infer", return_value=out) as m:
            res = llm.batch_infer(images, ["p"] * 2, model_key="HY", max_workers=2)

        self.assertEqual(res, out)
        self.assertEqual(m.call_count, 1)
        call_args, call_kwargs = m.call_args
        self.assertEqual(call_args[0], images)
        self.assertEqual(call_kwargs["model_key"], "HY")
        self.assertEqual(call_kwargs["max_workers"], 2)

    def test_request_image_dispatches_to_vllm(self):
        llm.set_engine("vllm")
        ret = {"result": "ok", "error": None}
        with mock.patch.object(vm, "_request_image_new", return_value=ret) as m:
            res = llm._request_image_new("prompt", "/tmp/a.png", "HY")

        self.assertEqual(res, ret)
        self.assertEqual(m.call_count, 1)
        call_args, call_kwargs = m.call_args
        self.assertEqual(call_args[0], "prompt")
        self.assertEqual(call_args[1], "/tmp/a.png")
        self.assertEqual(call_kwargs["model_key"], "HY")

    def test_request_dispatches_to_vllm(self):
        llm.set_engine("vllm")
        ret = {"result": "ok", "error": None}
        with mock.patch.object(vm, "request", return_value=ret) as m:
            res = llm.request("hi", "HY")

        self.assertEqual(res, ret)
        self.assertEqual(m.call_args, mock.call("hi", model_key="HY", thinking=False,
                                                append_ocr_instruction=True))


class TestBatchPerf(_EngineResetMixin, unittest.TestCase):
    """2026-08-17 性能调优：批内跳过逐页配置读取 + 模型推荐并发。"""

    def test_request_image_skips_config_when_batch_provides(self):
        # batch 提供 model_name+base_url 时不再逐页调 _vll_args（读 config.json）
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        with mock.patch.object(vm, "_vll_args",
                               side_effect=AssertionError("批次提供 base_url/model_name 时不应逐页读配置")), \
             mock.patch.object(vm._SESSION, "post", return_value=resp) as post:
            res = vm._request_image_new(
                "p", _FakeImg(), "HY", model_name="HY", base_url="http://127.0.0.1:8000"
            )
        self.assertIsNone(res["error"])
        self.assertEqual(res["result"], "ok")
        self.assertEqual(post.call_count, 1)
        url_args, _ = post.call_args
        self.assertIn("http://127.0.0.1:8000/v1/chat/completions", url_args[0])

    def test_batch_infer_resolves_workers_and_base_url_once(self):
        # 配置（模型名/base_url/推荐并发）批次开始解析一次，批内不再读
        args = ("C:/vllm/vllm.exe", {"host": "127.0.0.1", "port": 8000},
                "C:/models", {"HY": {"name": "HY", "mmproj": "", "workers": 6}})
        seen = []

        def fake_req(prompt, img, model_key, thinking=False, img_is_base64=False,
                     timeout=llm.REQUEST_TIMEOUT, model_name=None, base_url=None):
            seen.append((model_name, base_url))
            return {"img": img, "result": "ok", "error": None}

        with mock.patch.object(vm, "_vll_args", return_value=args) as vargs, \
             mock.patch.object(vm, "_request_image_new", side_effect=fake_req):
            out = vm.batch_infer(["/tmp/a.png", "/tmp/b.png"], ["p"] * 2, model_key="HY", max_workers=None)

        self.assertEqual(len(out), 2)
        self.assertEqual(vargs.call_count, 1, "批次内配置只应解析一次")
        self.assertTrue(
            all(n == "HY" and u == "http://127.0.0.1:8000" for n, u in seen),
            "每页请求都应拿到同一模型名与 base_url",
        )


if __name__ == "__main__":
    unittest.main()
