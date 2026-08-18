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


class TestModelIdMatches(unittest.TestCase):
    """_model_id_matches：llama-server 报告完整路径、config 存相对路径时的模型比对。

    2026-08-08 修复：矫正界面点击启动后 UI 显示未启动——旧实例存活时
    _probe_server 因路径格式差异把同一模型误判 mismatch 而中止。
    """

    def test_relative_name_matches_full_path_id(self):
        # config name 是相对路径（qwen3.5/xxx.gguf），llama-server 报告完整路径
        self.assertTrue(
            llm._model_id_matches(
                "qwen3.5/Qwen3.5-0.8B-Q8_0.gguf",
                ["E:/model/qwen3.5/Qwen3.5-0.8B-Q8_0.gguf"],
            )
        )

    def test_exact_full_path_still_matches(self):
        self.assertTrue(
            llm._model_id_matches(
                "E:/model/qwen3.5/Qwen3.5-0.8B-Q8_0.gguf",
                ["E:/model/qwen3.5/Qwen3.5-0.8B-Q8_0.gguf"],
            )
        )

    def test_backslash_and_case_insensitive(self):
        # Windows 反斜杠路径 + 大小写差异都应命中
        self.assertTrue(
            llm._model_id_matches(
                "qwen3.5\\Qwen3.5-0.8B-Q8_0.GGUF",
                ["E:/model/qwen3.5/Qwen3.5-0.8B-Q8_0.gguf"],
            )
        )

    def test_different_model_is_mismatch(self):
        self.assertFalse(
            llm._model_id_matches(
                "dots/dots.ocr.IQ4_XS.gguf",
                ["E:/model/qwen3.5/Qwen3.5-0.8B-Q8_0.gguf"],
            )
        )

    def test_empty_model_name_never_matches(self):
        self.assertFalse(llm._model_id_matches("", ["E:/model/x.gguf"]))
        self.assertFalse(llm._model_id_matches(None, ["E:/model/x.gguf"]))


class TestSessionRetry(unittest.TestCase):
    """_SESSION 连接级重试配置。

    2026-08-09 修复：llama-server 关闭空闲 keep-alive 连接后，复用 _SESSION 的
    下一次 POST 因陈旧连接直接 ConnectionError（requests 默认 Retry(0) 且 POST
    不在默认重试方法内）→ 矫正界面「重识别」偶发「无法连接本地 llama-server」。
    """

    def test_session_has_connection_retry(self):
        adapter = llm._SESSION.get_adapter("http://")
        retries = adapter.max_retries
        self.assertIsNotNone(retries, "_SESSION 应挂载带重试的 HTTPAdapter")
        self.assertGreaterEqual(retries.total, 1, "连接失败应至少重试 1 次")
        self.assertGreaterEqual(retries.connect, 1, "连接建立失败应重试")

    def test_retry_allows_post(self):
        # POST 不在 urllib3 默认重试方法（幂等集合）内，必须显式放行，
        # 否则陈旧 keep-alive 连接上的 OCR POST 不会重试
        adapter = llm._SESSION.get_adapter("http://")
        retries = adapter.max_retries
        self.assertIn("POST", retries.allowed_methods)

    def test_retry_does_not_retry_read_or_status(self):
        # 超时/4xx 不重试——由上层（_friendly_llm_error / 调用方）处理
        adapter = llm._SESSION.get_adapter("http://")
        retries = adapter.max_retries
        self.assertEqual(retries.read, 0)
        self.assertEqual(retries.status, 0)


class TestHealthTimeout(unittest.TestCase):
    """runserver 的 health 轮询超时：大模型加载可能超过 2 分钟（2026-08-09）。"""

    def test_health_timeout_at_least_300(self):
        self.assertGreaterEqual(llm._HEALTH_TIMEOUT, 300)


class TestStopserverFallback(unittest.TestCase):
    """stopserver 端口兜底：本进程无跟踪实例时按端口杀遗留/外部进程（2026-08-13）。"""

    def setUp(self):
        # 确保走 llama 分支（默认引擎）；清理此前测试可能遗留的 --engine 覆盖
        self._orig_engine = llm._ENGINE_OVERRIDE
        llm.set_engine(None)
        self.addCleanup(lambda: llm.set_engine(self._orig_engine))

    def test_kill_port_owner_parses_netstat_and_taskkills(self):
        import llamamanage

        netstat_out = (
            "  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       12345\r\n"
            "  TCP    [::]:8080              [::]:0                 LISTENING       67890\r\n"
            "  TCP    0.0.0.0:9999           0.0.0.0:0              LISTENING       11111\r\n"
        )
        calls = []

        def fake_check_output(args, **kw):
            self.assertEqual(args[0], "netstat")
            return netstat_out

        def fake_run(args, **kw):
            calls.append(args)
            return None

        orig_co = llamamanage.subprocess.check_output
        orig_run = llamamanage.subprocess.run
        llamamanage.subprocess.check_output = fake_check_output
        llamamanage.subprocess.run = fake_run
        try:
            self.assertTrue(llamamanage._kill_port_owner(8080))
        finally:
            llamamanage.subprocess.check_output = orig_co
            llamamanage.subprocess.run = orig_run
        self.assertEqual(len(calls), 2, "8080 上两个监听 PID 都应被 taskkill")
        for args in calls:
            self.assertEqual(args[0], "taskkill")
            self.assertIn("/F", args)

    def test_kill_port_owner_no_listener_returns_false(self):
        import llamamanage

        def fake_check_output(args, **kw):
            return "  TCP    0.0.0.0:9999           0.0.0.0:0              LISTENING       11111\r\n"

        orig_co = llamamanage.subprocess.check_output
        llamamanage.subprocess.check_output = fake_check_output
        try:
            self.assertFalse(llamamanage._kill_port_owner(8080))
        finally:
            llamamanage.subprocess.check_output = orig_co

    def test_stopserver_fallback_kills_port_owner(self):
        import llamamanage

        orig_proc = llamamanage._server_process
        orig_kill = llamamanage._kill_port_owner
        orig_port = llamamanage._server_port
        llamamanage._server_process = None
        llamamanage._kill_port_owner = lambda port: True
        llamamanage._server_port = lambda: "8080"
        try:
            self.assertTrue(llamamanage.stopserver())
        finally:
            llamamanage._server_process = orig_proc
            llamamanage._kill_port_owner = orig_kill
            llamamanage._server_port = orig_port


class TestServerArgSupport(unittest.TestCase):
    """_server_supports_arg：不同 llama-server 构建参数集不同，启动参数透传前探测。

    2026-08-17 修复：llama13 构建不支持 --max-tokens/--ngram-size/--window-size，
    DEFAULT_CONFIG 种子经 get_config() 递归合并进 llama_server_args 后透传，
    进程立即退出（error: invalid argument: --max-tokens，exit code 1）。
    """

    def setUp(self):
        self._cache_backup = dict(llm._ARG_HELP_CACHE)
        llm._ARG_HELP_CACHE.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        llm._ARG_HELP_CACHE.clear()
        llm._ARG_HELP_CACHE.update(self._cache_backup)

    def _help_run(self, stdout, flag="--max-tokens"):
        # 每次用独立 exe 名，避免 _ARG_HELP_CACHE 缓存命中上一次的 help 文本
        with mock.patch.object(
            llm.subprocess,
            "run",
            return_value=mock.Mock(stdout=stdout, stderr=""),
        ):
            return llm._server_supports_arg(f"fake-exe-{id(stdout)}", flag)

    def test_flag_present_in_help(self):
        self.assertTrue(
            self._help_run("-n, --predict, --n-predict N    number of tokens\n", "--predict")
        )
        self.assertTrue(
            self._help_run("--max-tokens N    max tokens to predict\n")
        )

    def test_flag_absent_from_help(self):
        self.assertFalse(self._help_run("-ngl, --gpu-layers N\n"))

    def test_similar_flags_do_not_false_positive(self):
        # --spec-ngram-size-n / --image-max-tokens 不应命中 --ngram-size / --max-tokens
        with mock.patch.object(
            llm.subprocess,
            "run",
            return_value=mock.Mock(stdout="--spec-ngram-size-n N\n--image-max-tokens N\n", stderr=""),
        ):
            self.assertFalse(llm._server_supports_arg("fake-exe", "--ngram-size"))
            self.assertFalse(llm._server_supports_arg("fake-exe", "--max-tokens"))

    def test_help_probe_failure_is_conservative(self):
        # 探测失败（exe 不存在等）不阻断启动：保守放行
        with mock.patch.object(llm.subprocess, "run", side_effect=OSError("no exe")):
            self.assertTrue(llm._server_supports_arg("missing-exe", "--max-tokens"))

    def test_runserver_skips_unsupported_flags_but_keeps_request_max_tokens(self):
        # 构建不支持三个参数时：argv 不含对应 flag，MAX_TOKENS（请求级）仍更新
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None  # 进程存活，走 health 轮询
        fake_resp = mock.Mock(status_code=200)
        cfg = {
            "llama_server_args": {
                "max_tokens": "4096",
                "ngram_size": "30",
                "window_size": "90",
            }
        }
        orig_max = llm.MAX_TOKENS
        orig_proc = llm._server_process
        llm._server_process = None
        try:
            with mock.patch.object(llm, "_reload_config",
                                   return_value=("exe", "mdir", {"HY": {"name": "HY.gguf"}}, "HY")), \
                 mock.patch.object(llm, "get_config", return_value=cfg), \
                 mock.patch.object(llm, "_probe_server", return_value="none"), \
                 mock.patch.object(llm, "_detect_gpu", return_value=("", "")), \
                 mock.patch.object(llm, "_server_supports_arg", return_value=False), \
                 mock.patch.object(llm.os.path, "exists", return_value=True), \
                 mock.patch.object(llm.subprocess, "Popen", return_value=fake_proc) as popen_mock, \
                 mock.patch.object(llm._SESSION, "get", return_value=fake_resp):
                ok = llm.runserver("HY")
                # 断言须在 finally 恢复 MAX_TOKENS 之前执行
                self.assertEqual(llm.MAX_TOKENS, 4096, "请求级 max_tokens 不受启动参数支持性影响")
        finally:
            llm.MAX_TOKENS = orig_max
            llm._server_process = orig_proc
        self.assertTrue(ok)
        args = popen_mock.call_args[0][0]
        self.assertNotIn("--max-tokens", args)
        self.assertNotIn("--ngram-size", args)
        self.assertNotIn("--window-size", args)

    def test_runserver_passes_supported_flags(self):
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        fake_resp = mock.Mock(status_code=200)
        cfg = {"llama_server_args": {"max_tokens": "4096", "ngram_size": "30", "window_size": "90"}}
        orig_max = llm.MAX_TOKENS
        orig_proc = llm._server_process
        llm._server_process = None
        try:
            with mock.patch.object(llm, "_reload_config",
                                   return_value=("exe", "mdir", {"HY": {"name": "HY.gguf"}}, "HY")), \
                 mock.patch.object(llm, "get_config", return_value=cfg), \
                 mock.patch.object(llm, "_probe_server", return_value="none"), \
                 mock.patch.object(llm, "_detect_gpu", return_value=("", "")), \
                 mock.patch.object(llm, "_server_supports_arg", return_value=True), \
                 mock.patch.object(llm.os.path, "exists", return_value=True), \
                 mock.patch.object(llm.subprocess, "Popen", return_value=fake_proc) as popen_mock, \
                 mock.patch.object(llm._SESSION, "get", return_value=fake_resp):
                ok = llm.runserver("HY")
        finally:
            llm.MAX_TOKENS = orig_max
            llm._server_process = orig_proc
        self.assertTrue(ok)
        args = popen_mock.call_args[0][0]
        self.assertIn("--max-tokens", args)
        self.assertIn("4096", args)
        self.assertIn("--ngram-size", args)
        self.assertIn("--window-size", args)


class TestPerfTuning(unittest.TestCase):
    """2026-08-17 性能调优：模型推荐并发 / 批次引擎钉扎 / --parallel 自适应 / Flash Attention。"""

    def setUp(self):
        self._orig_override = llm._ENGINE_OVERRIDE
        self._orig_batch = llm._BATCH_ENGINE
        self._orig_cache = llm._ENGINE_CACHE
        self._orig_ts = llm._ENGINE_CACHE_TS
        llm.set_engine(None)
        llm._BATCH_ENGINE = None
        self.addCleanup(self._restore)

    def _restore(self):
        llm.set_engine(self._orig_override)
        llm._BATCH_ENGINE = self._orig_batch
        llm._ENGINE_CACHE = self._orig_cache
        llm._ENGINE_CACHE_TS = self._orig_ts

    def test_resolve_workers_explicit_wins(self):
        # 显式传入的 max_workers 优先于模型推荐
        cfg = {"HY": {"workers": 6}}
        self.assertEqual(llm._resolve_workers(cfg, "HY", 2), 2)
        self.assertEqual(llm._resolve_workers(cfg, "HY", None), 6)

    def test_resolve_workers_fallback_to_3(self):
        # 模型未配置推荐并发 / 非法值 → 回退 3
        self.assertEqual(llm._resolve_workers({"HY": {}}, "HY", None), 3)
        self.assertEqual(llm._resolve_workers(None, "HY", None), 3)
        self.assertEqual(llm._resolve_workers({"HY": {"workers": "bad"}}, "HY", None), 3)
        self.assertEqual(llm._resolve_workers({"HY": {"workers": 0}}, "HY", None), 3)
        self.assertEqual(llm._resolve_workers({"HY": {"workers": 99}}, "HY", None), 3)
        self.assertEqual(llm._resolve_workers({}, "MISSING", 4), 4)

    def test_default_workers_reads_config(self):
        with mock.patch.object(llm, "get_config", return_value={"model_choices": {"QWEN2": {"workers": 4}}}):
            self.assertEqual(llm.default_workers("QWEN2"), 4)
        with mock.patch.object(llm, "get_config", return_value={"model_choices": {}}):
            self.assertEqual(llm.default_workers("QWEN2"), 3)
        with mock.patch.object(llm, "get_config", side_effect=OSError("no config")):
            self.assertEqual(llm.default_workers("QWEN2"), 3)

    def test_batch_engine_pinned_no_per_request_config_reads(self):
        # 批内每页的引擎分发命中 _BATCH_ENGINE 钉扎值：get_config 不应被调用
        # （2 秒 TTL 过期后旧行为会逐页重读 config.json）
        seen = []

        def fake_request(prompt, img, model_key, thinking=False, img_is_base64=False,
                         timeout=llm.REQUEST_TIMEOUT, model_name=None):
            seen.append(llm._active_engine())
            return {"img": img, "result": "ok", "error": None}

        with mock.patch.object(llm, "_reload_config", return_value=("exe", "mdir", {"HY": {"name": "HY.gguf"}}, "HY")), \
             mock.patch.object(llm, "get_config", side_effect=AssertionError("批内不应重读 config.json")), \
             mock.patch.object(llm, "_request_image_new", side_effect=fake_request):
            out = llm.batch_infer(["/tmp/a.png", "/tmp/b.png"], ["p"] * 2, model_key="HY", max_workers=2)

        self.assertEqual(len(out), 2)
        self.assertEqual(seen, ["llama", "llama"])
        self.assertIsNone(llm._BATCH_ENGINE, "批次结束应恢复钉扎值")

    def _run_server(self, sargs, detect_gpu=("", ""), flash_style=None, parallel=None):
        """mock 掉子进程/探测后执行 runserver，返回启动 argv。"""
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        fake_resp = mock.Mock(status_code=200)
        cfg = {"llama_server_args": sargs}
        orig_proc = llm._server_process
        llm._server_process = None
        try:
            with mock.patch.object(llm, "_reload_config",
                                   return_value=("exe", "mdir", {"HY": {"name": "HY.gguf"}}, "HY")), \
                 mock.patch.object(llm, "get_config", return_value=cfg), \
                 mock.patch.object(llm, "_probe_server", return_value="none"), \
                 mock.patch.object(llm, "_detect_gpu", return_value=detect_gpu), \
                 mock.patch.object(llm, "_server_flash_attn_style", return_value=flash_style), \
                 mock.patch.object(llm.os.path, "exists", return_value=True), \
                 mock.patch.object(llm.subprocess, "Popen", return_value=fake_proc) as popen_mock, \
                 mock.patch.object(llm._SESSION, "get", return_value=fake_resp):
                ok = llm.runserver("HY", parallel=parallel)
        finally:
            llm._server_process = orig_proc
        self.assertTrue(ok)
        return popen_mock.call_args[0][0]

    def test_runserver_parallel_min_of_config_and_workers(self):
        # 流程传入实际并发 3、配置 parallel 8 → 取 min=3（槽位不多于并发）
        args = self._run_server({"parallel": "8"}, parallel=3)
        self.assertEqual(args[args.index("--parallel") + 1], "3")

    def test_runserver_parallel_keeps_config_without_hint(self):
        # 无并发提示（GUI 手动启动）→ 用配置值 8
        args = self._run_server({"parallel": "8"})
        self.assertEqual(args[args.index("--parallel") + 1], "8")

    def test_runserver_parallel_default_4(self):
        # 配置缺失 → 默认 4（原默认 11 会让 KV cache 多占近 3 倍显存）
        args = self._run_server({})
        self.assertEqual(args[args.index("--parallel") + 1], "4")

    def test_runserver_flash_attn_bare_flag_with_gpu(self):
        # 老构建（裸标志语法）+ CUDA → 附加裸 --flash-attn
        args = self._run_server({}, detect_gpu=("CUDA", "NVIDIA GeForce RTX 3060"), flash_style="bare")
        self.assertIn("--flash-attn", args)

    def test_runserver_flash_attn_valued_uses_build_default(self):
        # 新构建（值形式 [on|off|auto]，默认 auto）→ 不传参数：
        # CUDA 支持时自动开启、不支持时安全回退（裸标志会让 llama-server 启动失败）
        args = self._run_server({}, detect_gpu=("CUDA", "NVIDIA"), flash_style="valued")
        self.assertNotIn("--flash-attn", args)

    def test_runserver_flash_attn_skipped_on_cpu(self):
        # 无 GPU → 不附加
        args = self._run_server({}, detect_gpu=("", ""), flash_style="bare")
        self.assertNotIn("--flash-attn", args)

    def test_runserver_flash_attn_disabled_by_config(self):
        # config 显式 flash_attn=0 → 即使 CUDA 也不附加
        args = self._run_server({"flash_attn": "0"}, detect_gpu=("CUDA", "NVIDIA"), flash_style="bare")
        self.assertNotIn("--flash-attn", args)

    def test_runserver_flash_attn_forced_on_valued_build(self):
        # 值形式构建 + flash_attn=1 → 按正确语法强制传 --flash-attn on
        args = self._run_server({"flash_attn": "1"}, detect_gpu=("CUDA", "NVIDIA"), flash_style="valued")
        self.assertEqual(args[args.index("--flash-attn") + 1], "on")

    def test_runserver_flash_attn_skipped_when_unknown_build(self):
        # 语法探测失败（未知构建）→ 保守不附加任何参数
        args = self._run_server({}, detect_gpu=("CUDA", "NVIDIA"), flash_style=None)
        self.assertNotIn("--flash-attn", args)


class TestFlashAttnStyle(unittest.TestCase):
    """_server_flash_attn_style：--flash-attn 语法形式探测（裸标志 vs on|off|auto 值）。

    2026-08-17 修复：llama13 构建的 --flash-attn 是值形式（[on|off|auto]），
    裸标志透传会让 llama-server 打印 usage 后退出（exit code 1）。
    """

    def setUp(self):
        self._cache_backup = dict(llm._ARG_HELP_CACHE)
        llm._ARG_HELP_CACHE.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        llm._ARG_HELP_CACHE.clear()
        llm._ARG_HELP_CACHE.update(self._cache_backup)

    def _probe(self, stdout, exe=None):
        # 每次用独立 exe 名，避免 _ARG_HELP_CACHE 缓存命中上一次的 help 文本
        exe = exe or f"fake-exe-{id(stdout)}"
        with mock.patch.object(
            llm.subprocess,
            "run",
            return_value=mock.Mock(stdout=stdout, stderr=""),
        ):
            return llm._server_flash_attn_style(exe)

    def test_old_build_bare_flag(self):
        self.assertEqual(
            self._probe("-fa, --flash-attn    Enable Flash Attention\n"), "bare"
        )

    def test_new_build_valued_flag(self):
        # llama13 等新构建：带 on|off|auto 值
        self.assertEqual(
            self._probe(
                "-fa,   --flash-attn [on|off|auto]       set Flash Attention use "
                "('on', 'off', or 'auto', default: 'auto')\n"
            ),
            "valued",
        )

    def test_flag_absent_returns_none(self):
        self.assertIsNone(self._probe("-ngl, --gpu-layers N\n"))

    def test_probe_failure_returns_none(self):
        with mock.patch.object(llm.subprocess, "run", side_effect=OSError("no exe")):
            self.assertIsNone(llm._server_flash_attn_style("missing-exe"))

    def test_help_cached_between_calls(self):
        stdout = "-fa, --flash-attn    Enable Flash Attention\n"
        exe = "cached-exe"
        with mock.patch.object(
            llm.subprocess, "run", return_value=mock.Mock(stdout=stdout, stderr="")
        ) as run_mock:
            self.assertEqual(llm._server_flash_attn_style(exe), "bare")
            self.assertEqual(llm._server_flash_attn_style(exe), "bare")
        self.assertEqual(run_mock.call_count, 1, "--help 应只探测一次并缓存")


if __name__ == "__main__":
    unittest.main()
