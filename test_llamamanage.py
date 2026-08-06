"""test_llamamanage.py — OCR 批处理性能/正确性单元测试。

覆盖：
- batch_infer 批次内只解析一次配置（模型名），不随页数重复读 config.json；
- 每个请求都复用共享 Session（keep-alive），不逐页新建连接；
- 单页失败不中断批次，结果按完成顺序返回。
"""

import unittest
from pathlib import Path
from unittest import mock

import llamamanage as llm


class _FakeResp:
    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": "识别结果"}}]}


class TestBatchInfer(unittest.TestCase):
    def test_config_resolved_once_per_batch(self):
        # 配置（模型名）在批次开始前解析一次；每页请求不再触发 get_config()
        images = [f"/tmp/fake_{i}.png" for i in range(5)]
        prompts = ["请识别"] * 5
        seen = []

        def fake_request(prompt, img, model_key, thinking=False, img_is_base64=False,
                         timeout=llm.REQUEST_TIMEOUT, model_name=None):
            seen.append((img, model_name))
            return {"img": img, "result": "识别结果", "error": None}

        with mock.patch.object(llm, "_reload_config", return_value=("exe", "mdir", {"HY": {"name": "HY.gguf"}}, "HY")) as reload_mock, \
             mock.patch.object(llm, "_request_image_new", side_effect=fake_request):
            out = llm.batch_infer(images, prompts, model_key="HY", max_workers=3)

        self.assertEqual(len(out), 5)
        self.assertEqual(reload_mock.call_count, 1, "批次内配置只应解析一次")
        self.assertTrue(all(mn == "HY.gguf" for _, mn in seen), "每页请求都应拿到同一模型名")

    def test_requests_reuse_shared_session(self):
        # 每个请求走 _SESSION.post（keep-alive），而不是新建 requests.post
        import tempfile as _tf

        tmp = _tf.mkdtemp()
        img = Path(tmp) / "p.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))

        with mock.patch.object(llm._SESSION, "post", return_value=_FakeResp()) as post_mock, \
             mock.patch.object(llm, "_reload_config", return_value=("exe", "mdir", {"HY": {"name": "HY.gguf"}}, "HY")):
            res = llm._request_image_new("请识别", str(img), "HY")

        self.assertIsNone(res["error"])
        self.assertEqual(post_mock.call_count, 1)
        url, kwargs = post_mock.call_args
        self.assertIn("/v1/chat/completions", url[0])
        data = kwargs["json"]
        content = data["messages"][0]["content"]
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_single_failure_does_not_abort_batch(self):
        images = ["/tmp/a.png", "/tmp/b.png", "/tmp/c.png"]

        def flaky(prompt, img, model_key, thinking=False, img_is_base64=False,
                  timeout=llm.REQUEST_TIMEOUT, model_name=None):
            if "b" in str(img):
                return {"img": img, "result": None, "error": "boom"}
            return {"img": img, "result": "ok", "error": None}

        with mock.patch.object(llm, "_request_image_new", side_effect=flaky), \
             mock.patch.object(llm, "_reload_config", return_value=("exe", "mdir", {"HY": {"name": "HY.gguf"}}, "HY")):
            out = llm.batch_infer(images, ["p"] * 3, max_workers=2)

        self.assertEqual(len(out), 3)
        errs = [r["error"] for r in out if r.get("error")]
        self.assertEqual(errs, ["boom"])


if __name__ == "__main__":
    unittest.main()
