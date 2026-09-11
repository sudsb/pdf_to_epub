"""test_llamamanage.py — OCR 批处理性能/正确性单元测试。

覆盖：
- batch_infer 批次内只解析一次配置（模型名），不随页数重复读 config.json；
- 每个请求都复用共享 Session（keep-alive），不逐页新建连接；
- 单页失败不中断批次，结果按完成顺序返回。
"""

import builtins
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

    def test_runserver_flash_attn_disabled_by_int_zero(self):
        # config flash_attn 为整数 0（configmanage.set_llama_server_arg 写入）→
        # 也须禁用。旧代码 `str(sargs.get("flash_attn") or "")` 里 int 0 为假值
        # → 回退 ""（auto）→ bare 构建误附加裸 --flash-attn，破坏禁用契约。
        # 修复后先归一为字符串再判空，int 0 → "0" → 命中禁用语（2026-09-09）。
        args = self._run_server(
            {"flash_attn": 0}, detect_gpu=("CUDA", "NVIDIA"), flash_style="bare"
        )
        self.assertNotIn("--flash-attn", args)

    def test_runserver_flash_attn_forced_on_valued_build(self):
        # 值形式构建 + flash_attn=1 → 按正确语法强制传 --flash-attn on
        args = self._run_server({"flash_attn": "1"}, detect_gpu=("CUDA", "NVIDIA"), flash_style="valued")
        self.assertEqual(args[args.index("--flash-attn") + 1], "on")

    def test_runserver_flash_attn_skipped_when_unknown_build(self):
        # 语法探测失败（未知构建）→ 保守不附加任何参数
        args = self._run_server({}, detect_gpu=("CUDA", "NVIDIA"), flash_style=None)
        self.assertNotIn("--flash-attn", args)


class TestFlashAttnIntegerOff(unittest.TestCase):
    """flash_attn 整数 0 必须禁用 Flash Attention（2026-09-09）。

    configmanage.set_llama_server_arg('flash_attn', 0) 写入整数 0。
    旧代码 `str(sargs.get("flash_attn") or "")`：int 0 为假值 → 回退 ""（auto），
    禁用契约在 bare 构建上失效（误附加 --flash-attn）。修复后先归一为字符串。
    """

    def _run_capture(self, sargs):
        """执行 runserver 并同时捕获 print 输出与启动 argv。"""
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        fake_resp = mock.Mock(status_code=200)
        cfg = {"llama_server_args": sargs}
        orig_proc = llm._server_process
        orig_max = llm.MAX_TOKENS
        llm._server_process = None
        printed = []
        captured = {}
        _orig_print = print

        def _cap_print(*a, **kw):
            printed.append(" ".join(str(x) for x in a))

        try:
            builtins.print = _cap_print
            with mock.patch.object(llm, "_reload_config",
                                   return_value=("exe", "mdir", {"HY": {"name": "HY.gguf"}}, "HY")), \
                 mock.patch.object(llm, "get_config", return_value=cfg), \
                 mock.patch.object(llm, "_probe_server", return_value="none"), \
                 mock.patch.object(llm, "_detect_gpu", return_value=("CUDA", "NVIDIA")), \
                 mock.patch.object(llm, "_server_flash_attn_style", return_value="bare"), \
                 mock.patch.object(llm.os.path, "exists", return_value=True), \
                 mock.patch.object(llm.subprocess, "Popen", return_value=fake_proc) as popen_mock, \
                 mock.patch.object(llm._SESSION, "get", return_value=fake_resp):
                llm.runserver("HY")
            captured["args"] = popen_mock.call_args[0][0]
        finally:
            builtins.print = _orig_print
            llm.MAX_TOKENS = orig_max
            llm._server_process = orig_proc
        captured["printed"] = printed
        return captured

    def test_int_zero_disables_flash_attn(self):
        # 整数 0 → 修正后归一为 "0" → 禁用：不附加 --flash-attn
        c = self._run_capture({"flash_attn": 0})
        self.assertNotIn("--flash-attn", c["args"])
        # 也不走 "enabled"/"auto" 打印路径（禁用时不打印 Flash Attention 消息）
        self.assertFalse(
            any("Flash Attention:" in p for p in c["printed"]),
            "int 0 禁用后不应打印 Flash Attention enabled/auto 消息",
        )

    def test_string_zero_still_disables_flash_attn(self):
        # 字符串 "0"（旧写法）行为不变：仍禁用
        c = self._run_capture({"flash_attn": "0"})
        self.assertNotIn("--flash-attn", c["args"])
        self.assertFalse(
            any("Flash Attention:" in p for p in c["printed"]),
            "string '0' 禁用后不应打印 Flash Attention enabled/auto 消息",
        )

    def test_missing_flash_attn_uses_bare_auto(self):
        # 缺省 → auto：bare 构建仍附加裸 --flash-attn（默认启用，行为不变）
        c = self._run_capture({})
        self.assertIn("--flash-attn", c["args"])
        self.assertTrue(
            any("Flash Attention: enabled" in p for p in c["printed"]),
            "缺省时 bare 构建应打印 enabled",
        )


class TestParallelPerSlotWarning(unittest.TestCase):
    """--parallel 平分 ctx_size → 每槽 n_ctx < 安全下限时输出警告（2026-09-09）。

    实测 16384//4=4096 → 长页截断；16384//2=8192 → 完整。安全下限 =
    _VISION_IMAGE_TOKEN_EST*2（8192）。仅警告，不修改配置。
    """

    def _run_capture(self, sargs, parallel=None):
        """执行 runserver 并捕获 print 输出与启动 argv。"""
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        fake_resp = mock.Mock(status_code=200)
        cfg = {"llama_server_args": sargs}
        orig_proc = llm._server_process
        orig_max = llm.MAX_TOKENS
        llm._server_process = None
        printed = []
        captured = {}
        _orig_print = print

        def _cap_print(*a, **kw):
            printed.append(" ".join(str(x) for x in a))

        try:
            builtins.print = _cap_print
            with mock.patch.object(llm, "_reload_config",
                                   return_value=("exe", "mdir", {"HY": {"name": "HY.gguf"}}, "HY")), \
                 mock.patch.object(llm, "get_config", return_value=cfg), \
                 mock.patch.object(llm, "_probe_server", return_value="none"), \
                 mock.patch.object(llm, "_detect_gpu", return_value=("", "")), \
                 mock.patch.object(llm, "_server_supports_arg", return_value=True), \
                 mock.patch.object(llm.os.path, "exists", return_value=True), \
                 mock.patch.object(llm.subprocess, "Popen", return_value=fake_proc) as popen_mock, \
                 mock.patch.object(llm._SESSION, "get", return_value=fake_resp):
                llm.runserver("HY", parallel=parallel)
            captured["args"] = popen_mock.call_args[0][0]
        finally:
            builtins.print = _orig_print
            llm.MAX_TOKENS = orig_max
            llm._server_process = orig_proc
        captured["printed"] = printed
        return captured

    def test_parallel_4_ctx_16384_warns(self):
        # 16384//4 = 4096 < 8192 → 每槽预算不足 → 警告
        c = self._run_capture({"ctx_size": "16384", "parallel": "4"})
        warns = [p for p in c["printed"] if "平分上下文" in p and "每槽仅约 4096" in p]
        self.assertTrue(warns, "parallel=4 + ctx=16384 应输出每槽上下文警告")

    def test_parallel_2_ctx_16384_no_warning(self):
        # 16384//2 = 8192 >= 8192 → 每槽预算充足 → 不警告
        c = self._run_capture({"ctx_size": "16384", "parallel": "2"})
        warns = [p for p in c["printed"] if "平分上下文" in p]
        self.assertFalse(warns, "parallel=2 + ctx=16384 不应警告")

    def test_parallel_1_ctx_16384_no_warning(self):
        # 16384//1 = 16384 >= 8192 → 不警告
        c = self._run_capture({"ctx_size": "16384", "parallel": "1"})
        warns = [p for p in c["printed"] if "平分上下文" in p]
        self.assertFalse(warns, "parallel=1 + ctx=16384 不应警告")


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


class TestKillStaleLlamaOnPort(unittest.TestCase):
    """runserver 模型不符时跨进程清理旧 llama-server（仅杀映像名匹配的进程）。"""

    NETSTAT_TWO = (
        "  TCP    127.0.0.1:8080           0.0.0.0:0              LISTENING       111\n"
        "  TCP    127.0.0.1:8080           0.0.0.0:0              LISTENING       222\n"
        "  TCP    127.0.0.1:9090           0.0.0.0:0              LISTENING       333\n"
    )

    def _patch_subprocess(self, netstat_outputs, tasklist_by_pid, kill_ok=True):
        """统一打桩：check_output 按调用序返回 netstat/tasklist 输出，run 记录 taskkill。"""
        killed = []
        outputs = list(netstat_outputs)

        def fake_check_output(cmd, **kw):
            if cmd[0] == "netstat":
                return outputs.pop(0) if outputs else ""
            if cmd[0] == "tasklist":
                pid = cmd[2].split()[-1]
                return tasklist_by_pid.get(pid, "")
            raise AssertionError(f"unexpected cmd {cmd}")

        def fake_run(cmd, **kw):
            if cmd[0] == "taskkill":
                pid = cmd[2]
                if not kill_ok:
                    raise OSError("kill failed")
                killed.append(pid)
                return mock.Mock(returncode=0)
            raise AssertionError(f"unexpected cmd {cmd}")

        return (
            mock.patch.object(llm.subprocess, "check_output", side_effect=fake_check_output),
            mock.patch.object(llm.subprocess, "run", side_effect=fake_run),
            killed,
        )

    def test_kills_only_llama_server_listener(self):
        # 8080 上两个监听进程：111 是 llama-server.exe → 杀；222 是 ollama.exe → 不动
        tasklist = {
            "111": '"llama-server.exe","111","Console","1","123,456 K"\n',
            "222": '"ollama.exe","222","Console","1","999,999 K"\n',
            "333": '"chrome.exe","333","Console","1","10 K"\n',
        }
        # 第一次 netstat：初始监听；第二次（释放轮询）：空 → 端口已释放
        co, run, killed = self._patch_subprocess([self.NETSTAT_TWO, ""], tasklist)
        with co, run:
            self.assertTrue(llm._kill_stale_llama_on_port(8080))
        self.assertEqual(killed, ["111"], "只应结束 llama-server 进程")

    def test_non_llama_listener_not_killed(self):
        # 端口被其他服务占用时不误杀，返回 False
        tasklist = {"111": '"ollama.exe","111","Console","1","1 K"\n'}
        co, run, killed = self._patch_subprocess([self.NETSTAT_TWO], tasklist)
        with co, run:
            self.assertFalse(llm._kill_stale_llama_on_port(8080))
        self.assertEqual(killed, [])

    def test_kill_failure_returns_false(self):
        # taskkill 失败（权限等）→ False
        tasklist = {"111": '"llama-server.exe","111","Console","1","1 K"\n'}
        co, run, killed = self._patch_subprocess([self.NETSTAT_TWO], tasklist, kill_ok=False)
        with co, run:
            self.assertFalse(llm._kill_stale_llama_on_port(8080))
        self.assertEqual(killed, [])

    def test_no_listener_returns_false(self):
        co, run, killed = self._patch_subprocess([""], {})
        with co, run:
            self.assertFalse(llm._kill_stale_llama_on_port(8080))
        self.assertEqual(killed, [])


class TestRunserverStaleInstance(unittest.TestCase):
    """mismatch 分支：本进程句柄管不到旧实例时按端口自动清理。"""

    def test_own_handle_stops_and_proceeds(self):
        proc = mock.Mock()
        proc.poll.return_value = None  # 本进程启动的实例还活着
        probes = iter(["none"])
        with mock.patch.object(llm, "_server_process", proc), \
             mock.patch.object(llm, "stopserver") as stop_mock, \
             mock.patch.object(llm, "_probe_server", side_effect=lambda *a: next(probes)), \
             mock.patch.object(llm, "time") as tmock:
            tmock.sleep = lambda *_: None
            self.assertTrue(llm._handle_stale_instance("need.gguf"))
            stop_mock.assert_called_once()

    def test_external_instance_autokilled(self):
        # _server_process 为空（GUI 子进程场景）：probe 仍 mismatch → 按端口杀成功 → 继续
        probes = iter(["mismatch"])
        with mock.patch.object(llm, "_server_process", None), \
             mock.patch.object(llm, "_probe_server", side_effect=lambda *a: next(probes)), \
             mock.patch.object(llm, "_kill_stale_llama_on_port", return_value=True) as kill_mock:
            self.assertTrue(llm._handle_stale_instance("need.gguf"))
            kill_mock.assert_called_once()

    def test_unkillable_external_aborts(self):
        # 端口被非 llama-server 进程占用且无法清理 → False（中止）
        probes = iter(["mismatch"])
        with mock.patch.object(llm, "_server_process", None), \
             mock.patch.object(llm, "_probe_server", side_effect=lambda *a: next(probes)), \
             mock.patch.object(llm, "_kill_stale_llama_on_port", return_value=False):
            self.assertFalse(llm._handle_stale_instance("need.gguf"))


class TestTruncationWarning(unittest.TestCase):
    """_truncation_warning：finish_reason=length 的根因诊断（2026-09-07）。

    llama-server 在「触顶请求级 max_tokens」与「服务端 n_ctx 被图片 token 占满」
    两种情况下都报 finish_reason=length——视觉模型每页图片常占数千 prompt token，
    实际生成预算 = n_ctx - prompt_tokens。本警告用 usage 区分并给出可执行建议。
    """

    def test_ctx_full_branch(self):
        # 上下文被图片+生成占满：pt + ct >= n_ctx → 指向 ctx_size
        w = llm._truncation_warning(
            "<Item>", {"usage": {"prompt_tokens": 7700, "completion_tokens": 900}}, ctx=8192
        )
        self.assertIn("服务端上下文已满", w)
        self.assertIn("ctx_size=8192", w)
        self.assertIn("占 7700", w)
        self.assertIn("生成预算 ≈ 492 token", w)  # 8192 - 7700
        self.assertIn("调大 ctx_size", w)
        self.assertIn("调大 max_tokens 无效", w)
        self.assertNotIn("接近请求级上限", w)
        self.assertNotIn("请检查该页内容", w)  # 有 usage 就不走兜底段

    def test_ctx_full_custom_hint(self):
        # vLLM 走 vllm_server_args.max_model_len 提示词（pt+ct>=ctx 才入此分支）
        w = llm._truncation_warning(
            "<Item>", {"usage": {"prompt_tokens": 31000, "completion_tokens": 2000}}, ctx=32768,
            ctx_hint="vllm_server_args.max_model_len",
        )
        self.assertIn("vllm_server_args.max_model_len=32768", w)
        self.assertIn("生成预算 ≈ 1768 token", w)

    def test_request_cap_branch(self):
        # 未触 n_ctx → 指向请求级 max_tokens
        w = llm._truncation_warning(
            "<Item>", {"usage": {"prompt_tokens": 5000, "completion_tokens": 1000}}, ctx=16384
        )
        self.assertIn("接近请求级上限 max_tokens=8192", w)
        self.assertIn("调大 max_tokens", w)
        self.assertIn("usage={prompt:5000, completion:1000}", w)
        self.assertNotIn("服务端上下文已满", w)

    def test_ctx_unknown_hints_stale_server(self):
        # ctx=None（探测失败或未记录）→ 提示残留服务可能截断，不再误报请求级上限
        # （2026-09-07：请求级上限只在该页输出真实触顶 max_tokens 时才提示）
        w = llm._truncation_warning(
            "<Item>", {"usage": {"prompt_tokens": 6000, "completion_tokens": 3000}}, ctx=None
        )
        self.assertIn("无法确认服务端上下文大小", w)
        self.assertNotIn("接近请求级上限", w)

    def test_no_usage_fallback(self):
        # 响应无 usage 信息 → 保留原警告文本
        w = llm._truncation_warning("<Item>", {}, ctx=8192)
        self.assertIn("hit max_tokens=8192 (finish_reason=length)", w)
        self.assertIn("输出可能被截断，请检查该页内容", w)

    def test_usage_wrong_types_fallback(self):
        # usage 键缺失/类型不对 → 兜底
        w = llm._truncation_warning("<Item>", {"usage": {"prompt_tokens": "7700"}}, ctx=8192)
        self.assertIn("请检查该页内容", w)
        self.assertNotIn("服务端上下文已满", w)

    def test_truncation_ctx_size_reads_config(self):
        # _truncation_ctx_size：从 llama_server_args.ctx_size 取 int；缺失 → None
        with mock.patch.object(
            llm, "get_config",
            return_value={"llama_server_args": {"ctx_size": "16384"}},
        ):
            self.assertEqual(llm._truncation_ctx_size(), 16384)
        with mock.patch.object(llm, "get_config", return_value={"llama_server_args": {}}):
            self.assertIsNone(llm._truncation_ctx_size())
        with mock.patch.object(
            llm, "get_config",
            side_effect=RuntimeError("boom"),
        ):
            self.assertIsNone(llm._truncation_ctx_size())


class TestServerCtxTracking(unittest.TestCase):
    """_record_server_ctx / _truncation_ctx_size：真实服务端 n_ctx 优先（2026-09-07）。

    复用的残留旧进程可能以旧参数启动（n_ctx 远小于配置 ctx_size），截断警告
    必须知道真实 n_ctx 才能正确分流为「服务端上下文已满」而非误报请求级上限。
    """

    def setUp(self):
        self._saved = llm._SERVER_CTX
        self.addCleanup(self._restore)

    def _restore(self):
        llm._SERVER_CTX = self._saved

    def test_record_server_ctx_parses_slots(self):
        resp = mock.Mock()
        resp.json.return_value = [{"n_ctx": 2048}]
        with mock.patch.object(llm._SESSION, "get", return_value=resp) as g:
            self.assertEqual(llm._record_server_ctx(), 2048)
        g.assert_called_once()
        self.assertEqual(llm._SERVER_CTX, 2048)

    def test_record_server_ctx_empty_slots_keeps_old(self):
        llm._SERVER_CTX = 777
        resp = mock.Mock()
        resp.json.return_value = []
        with mock.patch.object(llm._SESSION, "get", return_value=resp):
            self.assertIsNone(llm._record_server_ctx())
        self.assertEqual(llm._SERVER_CTX, 777)  # 失败不改旧值

    def test_record_server_ctx_exception_keeps_old(self):
        llm._SERVER_CTX = 777
        with mock.patch.object(
            llm._SESSION, "get", side_effect=ConnectionError("refused")
        ):
            self.assertIsNone(llm._record_server_ctx())
        self.assertEqual(llm._SERVER_CTX, 777)

    def test_truncation_ctx_size_prefers_server_ctx(self):
        # 运行中服务真实 n_ctx（残留旧进程）优先于配置
        llm._SERVER_CTX = 2048
        with mock.patch.object(
            llm, "get_config",
            return_value={"llama_server_args": {"ctx_size": "16384"}},
        ):
            self.assertEqual(llm._truncation_ctx_size(), 2048)

    def test_truncation_ctx_size_falls_back_config(self):
        llm._SERVER_CTX = None
        with mock.patch.object(
            llm, "get_config",
            return_value={"llama_server_args": {"ctx_size": "16384"}},
        ):
            self.assertEqual(llm._truncation_ctx_size(), 16384)
        with mock.patch.object(llm, "get_config", return_value={"llama_server_args": {}}):
            self.assertIsNone(llm._truncation_ctx_size())

    def test_truncation_ctx_size_config_raises(self):
        llm._SERVER_CTX = None
        with mock.patch.object(
            llm, "get_config", side_effect=RuntimeError("boom")
        ):
            self.assertIsNone(llm._truncation_ctx_size())


class TestServerReuseRestart(unittest.TestCase):
    """runserver 复用分支：真实 n_ctx 低于配置 ctx_size 时自动重启残留进程
    （2026-09-07）。

    此前仅提示不处理——残留旧进程（未传 --ctx-size，n_ctx 落模型原生上下文，
    如 2048）因模型名匹配被无限复用，OCR 每页被小上下文静默截断
    （实测 usage 1785+263=2048 整，输出仅剩一两百 token）。现在探测到
    n_ctx < 配置 ctx_size 即自动停旧进程并重启采用当前配置。
    """

    def setUp(self):
        self._saved_ctx = llm._SERVER_CTX
        self._saved_proc = llm._server_process

        def _restore():
            llm._SERVER_CTX = self._saved_ctx
            llm._server_process = self._saved_proc

        self.addCleanup(_restore)
        llm._SERVER_CTX = None
        llm._server_process = None

    def _base_patches(self):
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(mock.patch.object(llm, "_active_engine", return_value="llama"))
        stack.enter_context(
            mock.patch.object(
                llm,
                "_reload_config",
                return_value=(
                    "E:/x/t/llama-server.exe",
                    "E:/model",
                    {"DOTS8": {"name": "dots.ocr.Q8_0.gguf", "mmproj": "dots_p.gguf"}},
                    "DOTS8",
                ),
            )
        )
        stack.enter_context(mock.patch("os.path.exists", return_value=True))
        stack.enter_context(mock.patch.object(llm, "_server_supports_arg", return_value=True))
        stack.enter_context(mock.patch.object(llm, "_detect_gpu", return_value=(None, None)))
        stack.enter_context(
            mock.patch.object(
                llm, "get_config", return_value={"llama_server_args": {"ctx_size": "16384"}}
            )
        )
        return stack

    def test_server_ctx_below_config_triggers_restart(self):
        # 残留 2048 服务（模型名匹配被复用）→ 低于配置 16384 → 自动重启
        with self._base_patches():
            with mock.patch.object(llm, "_probe_server", return_value="match"):
                with mock.patch.object(
                    llm, "_record_server_ctx",
                    side_effect=lambda: setattr(llm, "_SERVER_CTX", 2048),
                ):
                    with mock.patch.object(
                        llm, "_handle_stale_instance", return_value=True
                    ) as hsi:
                        with mock.patch.object(
                            llm, "subprocess", wraps=llm.subprocess
                        ) as sp:
                            sp.Popen = mock.Mock(return_value=mock.Mock(poll=lambda: None))
                            health = mock.Mock(status_code=200)
                            with mock.patch.object(llm._SESSION, "get", return_value=health):
                                ok = llm.runserver("DOTS8")
        self.assertTrue(ok)
        hsi.assert_called_once()  # 残留进程被清理（关键断言）
        sp.Popen.assert_called_once()  # 随后按当前配置重新启动

    def test_server_ctx_matches_config_reuses_without_restart(self):
        # 服务 n_ctx == 配置 → 普通复用，不清理不重启
        with self._base_patches():
            with mock.patch.object(llm, "_probe_server", return_value="match"):
                with mock.patch.object(
                    llm, "_record_server_ctx",
                    side_effect=lambda: setattr(llm, "_SERVER_CTX", 16384),
                ):
                    with mock.patch.object(
                        llm, "_handle_stale_instance", return_value=True
                    ) as hsi:
                        ok = llm.runserver("DOTS8")
        self.assertTrue(ok)
        hsi.assert_not_called()

    def test_server_ctx_probe_failure_warns_and_reuses(self):
        # /slots 探测失败（_SERVER_CTX 保持 None）→ 打提示但照常复用
        with self._base_patches():
            with mock.patch.object(llm, "_probe_server", return_value="match"):
                with mock.patch.object(llm, "_record_server_ctx", return_value=None):
                    with mock.patch.object(
                        llm, "_handle_stale_instance", return_value=True
                    ) as hsi:
                        ok = llm.runserver("DOTS8")
        self.assertTrue(ok)
        hsi.assert_not_called()

    def test_truncation_warning_ctx_none_hints_stale_server(self):
        # ctx=None（无法确认服务端上下文）→ 提示残留服务可能截断，不再误报请求级上限
        w = llm._truncation_warning(
            "<Item>", {"usage": {"prompt_tokens": 1785, "completion_tokens": 263}}
        )
        self.assertIn("无法确认服务端上下文大小", w)
        self.assertIn("残留旧服务上下文偏小", w)
        self.assertNotIn("接近请求级上限", w)


class TestStartupVisionBalanceWarning(unittest.TestCase):
    """runserver 启动时视觉 OCR 生成余量提醒（2026-09-09）。

    旧阈值 ctx_size < max_tokens + 1024 对视觉模型过窄（图片 token 实际 4600-8700），
    改用 _VISION_IMAGE_TOKEN_EST=4096 估算后能正确触发警告。
    """

    def _runserver_with_sargs(self, sargs):
        """mock runserver 返回捕获的 print 输出。"""
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        fake_resp = mock.Mock(status_code=200)
        cfg = {"llama_server_args": sargs}
        orig_proc = llm._server_process
        orig_max = llm.MAX_TOKENS
        llm._server_process = None
        printed = []
        _orig_print = print
        try:
            # 劫持 print 以捕获警告
            builtins.print = lambda *a, **kw: printed.append(" ".join(str(x) for x in a))
            with mock.patch.object(llm, "_reload_config",
                                   return_value=("exe", "mdir", {"HY": {"name": "HY.gguf"}}, "HY")), \
                 mock.patch.object(llm, "get_config", return_value=cfg), \
                 mock.patch.object(llm, "_probe_server", return_value="none"), \
                 mock.patch.object(llm, "_detect_gpu", return_value=("", "")), \
                 mock.patch.object(llm, "_server_supports_arg", return_value=True), \
                 mock.patch.object(llm.os.path, "exists", return_value=True), \
                 mock.patch.object(llm.subprocess, "Popen", return_value=fake_proc), \
                 mock.patch.object(llm._SESSION, "get", return_value=fake_resp):
                llm.runserver("HY")
        finally:
            builtins.print = _orig_print
            llm.MAX_TOKENS = orig_max
            llm._server_process = orig_proc
        return printed

    def test_warns_when_ctx_insufficient_for_image_tokens(self):
        # ctx_size=12288, max_tokens=8192 → 旧阈值 8192+1024=9216 不触发（12288>9216）
        # 新阈值 8192+4096=12288 → 12288<12288 不触发（边界）
        # ctx_size=12287 → 新阈值触发（图片 token 会让生成预算不足）
        printed = self._runserver_with_sargs({"max_tokens": "8192", "ctx_size": "12287"})
        warns = [p for p in printed if "图片 token" in p]
        self.assertTrue(warns, "ctx_size 不足时应输出图片 token 余量警告")

    def test_no_warning_when_ctx_large_enough(self):
        # ctx_size=16384, max_tokens=8192 → 16384 >= 8192+4096=12288 → 不警告
        printed = self._runserver_with_sargs({"max_tokens": "8192", "ctx_size": "16384"})
        warns = [p for p in printed if "图片 token" in p]
        self.assertFalse(warns, "ctx_size 充足时不应输出图片 token 警告")

    def test_old_threshold_would_miss_warning(self):
        # ctx_size=9000, max_tokens=8192
        # 旧阈值 8192+1024=9216 → 9000 < 9216 也会触发（恰好）
        # 新阈值 8192+4096=12288 → 9000 < 12288 更宽触发
        # 确认新阈值比旧阈值更宽：用旧阈值刚好不触发的值
        # ctx_size=9217 > 9216（旧阈值不触发），但 9217 < 12288（新阈值触发）
        printed = self._runserver_with_sargs({"max_tokens": "8192", "ctx_size": "9217"})
        warns = [p for p in printed if "图片 token" in p]
        self.assertTrue(warns, "新阈值应比旧阈值更宽地捕获图片 token 不足场景")


class TestVisionImageTokenEst(unittest.TestCase):
    """_VISION_IMAGE_TOKEN_EST 常量合理性（2026-09-09）。"""

    def test_constant_value(self):
        # 保守低端估算，实际视觉模型 4600-8700 token/页
        self.assertGreaterEqual(llm._VISION_IMAGE_TOKEN_EST, 2048)
        self.assertLessEqual(llm._VISION_IMAGE_TOKEN_EST, 16384)


class TestStaleRestartBeforeInference(unittest.TestCase):
    """Change D 验证：stale-server 重启在 runserver 内完成，在 batch_infer
    发起页面请求之前（runserver 是 pipeline 的前置步骤）。

    补充断言：重启后 _record_server_ctx 被再次调用以重新探测真实 n_ctx，
    确保重启后的新进程上下文被正确记录。
    """

    def setUp(self):
        self._saved_ctx = llm._SERVER_CTX
        self._saved_proc = llm._server_process

        def _restore():
            llm._SERVER_CTX = self._saved_ctx
            llm._server_process = self._saved_proc

        self.addCleanup(_restore)
        llm._SERVER_CTX = None
        llm._server_process = None

    def test_record_server_ctx_called_after_restart_for_reprobe(self):
        """残留进程重启后，_record_server_ctx 应被再次调用以记录新进程的 n_ctx。"""
        record_calls = []

        def fake_record():
            # 第一次调用（复用分支）：模拟残留 2048
            if len(record_calls) == 0:
                record_calls.append("first")
                llm._SERVER_CTX = 2048
                return 2048
            # 第二次调用（新进程启动后）：模拟新进程 16384
            record_calls.append("second")
            llm._SERVER_CTX = 16384
            return 16384

        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(llm, "_active_engine", return_value="llama"))
            stack.enter_context(mock.patch.object(
                llm, "_reload_config",
                return_value=("E:/x/t/llama-server.exe", "E:/model",
                              {"M": {"name": "m.gguf", "mmproj": ""}}, "M")))
            stack.enter_context(mock.patch("os.path.exists", return_value=True))
            stack.enter_context(mock.patch.object(llm, "_server_supports_arg", return_value=True))
            stack.enter_context(mock.patch.object(llm, "_detect_gpu", return_value=(None, None)))
            stack.enter_context(mock.patch.object(
                llm, "get_config", return_value={"llama_server_args": {"ctx_size": "16384"}}))
            stack.enter_context(mock.patch.object(llm, "_probe_server", return_value="match"))
            stack.enter_context(mock.patch.object(llm, "_record_server_ctx", side_effect=fake_record))
            stack.enter_context(mock.patch.object(llm, "_handle_stale_instance", return_value=True))
            fake_proc = mock.Mock()
            fake_proc.poll.return_value = None
            stack.enter_context(mock.patch.object(
                llm, "subprocess", wraps=llm.subprocess))
            llm.subprocess.Popen = mock.Mock(return_value=fake_proc)
            health = mock.Mock(status_code=200)
            stack.enter_context(mock.patch.object(llm._SESSION, "get", return_value=health))
            ok = llm.runserver("M")

        self.assertTrue(ok)
        self.assertEqual(record_calls, ["first", "second"],
                         "runserver 复用分支 + 新进程启动后各调一次 _record_server_ctx")


class TestTruncationWarningBudgetMessage(unittest.TestCase):
    """Change B 补充：context-bound 分支明确报告生成预算且提示 max_tokens 无效。"""

    def test_budget_reported_in_message(self):
        w = llm._truncation_warning(
            "test", {"usage": {"prompt_tokens": 7500, "completion_tokens": 700}}, ctx=8192
        )
        self.assertIn("生成预算 ≈ 692 token", w)  # 8192 - 7500

    def test_max_tokens_ineffective_stated(self):
        w = llm._truncation_warning(
            "test", {"usage": {"prompt_tokens": 7500, "completion_tokens": 800}}, ctx=8192
        )
        self.assertIn("调大 max_tokens 无效", w)
        self.assertIn("调大 ctx_size", w)
