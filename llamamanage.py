import base64
import os
import re
import subprocess
import threading
import time

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    _REQUESTS_AVAILABLE = True
except Exception:
    requests = None
    HTTPAdapter = None
    Retry = None
    _REQUESTS_AVAILABLE = False

from configmanage import get_config

_server_process: subprocess.Popen | None = None

# 复用 HTTP 连接（keep-alive）：多页 OCR 时避免每页都新建 TCP 连接/握手
if _REQUESTS_AVAILABLE:
    _SESSION = requests.Session()
    # create adapter explicitly and set max_retries to ensure adapter.max_retries is present
    adapter = HTTPAdapter(pool_connections=20, pool_maxsize=64)
    adapter.max_retries = Retry(
        total=2,
        connect=2,
        read=0,
        status=0,
        backoff_factor=0.5,
        allowed_methods=frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS", "TRACE", "POST"}),
    )
    _SESSION.mount("http://", adapter)
    _SESSION.mount("https://", adapter)
else:
    class _DummyAdapter:
        def __init__(self):
            class _R:
                def __init__(self):
                    self.total = 2
                    self.connect = 2
                    self.read = 0
                    self.status = 0
                    self.allowed_methods = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS", "TRACE", "POST"})

            self.max_retries = _R()

    class DummySession:
        def mount(self, prefix, adapter):
            return None

        def get_adapter(self, url):
            return _DummyAdapter()

        def get(self, *args, **kwargs):
            raise RuntimeError("requests not installed; network calls are disabled in this environment")

        def post(self, *args, **kwargs):
            raise RuntimeError("requests not installed; network calls are disabled in this environment")

    _SESSION = DummySession()

# 单次推理请求超时（秒）。300dpi 页面会被编码成约 8700 个图像 token（200dpi 约 4600），
# 4 并发时图片编码+生成可能超过 1 分钟，默认 60s 会误杀正常请求。
REQUEST_TIMEOUT = 600

# 单页 OCR 输出上限（token）。一页识别文本约 400-900 tokens（≈2800 汉字），
# 默认 8192 更能容纳高 token 的页面与思考链场景；仍限制上界以防单页生成跑飞。
# 可通过 config.json 的 llama_server_args.max_tokens 覆盖（见 USAGE.md）。
MAX_TOKENS = 8192
# runserver 等待模型加载完成的 health 轮询超时（秒）。大模型（含视觉投影器）
# 从磁盘加载到显存可能远超 2 分钟，此期间 /health 一直返回 503；超时过短会
# 误判启动失败（矫正界面表现为「服务未启动」，而进程其实仍在加载）。
_HEALTH_TIMEOUT = 300

# llama-server 构建参数支持性缓存：exe 路径 → --help 输出文本。
# 不同版本/构建的参数集不同（如 llama13 构建无 --max-tokens，仅 -n/--predict），
# 若把 config 默认合并来的参数透传，进程会立即退出（error: invalid argument）。
_ARG_HELP_CACHE: dict = {}

# 统一 OCR 提示词：强调逐行完整、逐字输出，缓解 0.8B 模型跳字/漏行
# （实测最差页字符数 +20%，无速度代价）。所有调用方共用此常量，禁止各自
# 内联自定义 OCR 提示词；llamamanage 还会在非 thinking 模式下追加
# "\n按原文原格式输出" 后缀。
OCR_PROMPT = "OCR this image,Output the original text in its original format without any missing or omitted characters. Reply in Simplified Chinese"


# ---- 推理引擎分发（2026-08-09）：llama.cpp（本模块）| vLLM-Omni（vllmmanage）----
# config.json 顶层 engine 键选择引擎（"llama" 默认 | "vllm"）；CLI --engine 可
# 经 set_engine() 临时覆盖（不写盘）。分发只在本模块各公开函数入口做一次，
# engine=llama 时 llama.cpp 代码路径逐字节不变。
_ENGINE_OVERRIDE: str | None = None
_ENGINE_CACHE: str | None = None
_ENGINE_CACHE_TS = 0.0
_ENGINE_CACHE_TTL = 2.0  # 秒：批次内避免每页重复读 config.json

# 批次内引擎钉扎（2026-08-17 性能调优）：batch_infer 开始解析一次引擎，批内
# 每页请求的引擎分发直接命中此钉扎值——_ENGINE_CACHE 的 2 秒 TTL 在长批次
# （数百页、数十分钟）中会反复过期，导致每 2 秒就重读一次 config.json
# （读文件 + 全局锁 + 递归合并校验）。仅批处理主线程写入、工作线程只读，
# 嵌套批次（批内再开批）保存/恢复旧值。
_BATCH_ENGINE: str | None = None


def set_engine(engine: str | None) -> None:
    """CLI --engine 临时覆盖引擎选择（不写 config.json）。None 恢复按配置。"""
    global _ENGINE_OVERRIDE, _ENGINE_CACHE
    _ENGINE_OVERRIDE = engine
    _ENGINE_CACHE = None


def _active_engine() -> str:
    """返回当前推理引擎：'llama'（llama.cpp，默认）或 'vllm'（vLLM-Omni）。"""
    global _ENGINE_CACHE, _ENGINE_CACHE_TS
    if _BATCH_ENGINE:
        return _BATCH_ENGINE
    if _ENGINE_OVERRIDE:
        return _ENGINE_OVERRIDE
    now = time.monotonic()
    if _ENGINE_CACHE is not None and now - _ENGINE_CACHE_TS < _ENGINE_CACHE_TTL:
        return _ENGINE_CACHE
    try:
        eng = get_config(show_dialogs=False).get("engine", "llama") or "llama"
    except Exception:
        eng = "llama"
    _ENGINE_CACHE = eng
    _ENGINE_CACHE_TS = now
    return eng


def _vllm_module():
    """惰性导入 vllmmanage（engine=llama 时不引入额外依赖/开销）。"""
    import vllmmanage

    return vllmmanage


def _reload_config():
    cfg = get_config()
    return (
        cfg.get("llama_server"),
        cfg.get("models_dir"),
        cfg.get("model_choices"),
        cfg.get("selected_model"),
    )


def _resolve_workers(
    model_cfg: dict | None, model_key: str, max_workers: int | None
) -> int:
    """批处理并发数解析（2026-08-17 性能调优）。

    优先级：显式传入的 max_workers > 模型推荐并发 model_choices.<key>.workers
    （设置页「模型管理」可调）> 兜底 3。非法值（非整数 / <1 / >64）逐级回退，
    保证并发数始终落在安全区间（过高并发会让多槽位 KV 缓存溢出到 CPU，单张
    耗时反而大涨，见 batch_infer 注释）。
    """
    if max_workers is not None:
        try:
            w = int(max_workers)
            if 1 <= w <= 64:
                return w
        except (TypeError, ValueError):
            pass
    try:
        w = int(((model_cfg or {}).get(model_key) or {}).get("workers") or 0)
        if 1 <= w <= 64:
            return w
    except (TypeError, ValueError):
        pass
    return 3


def default_workers(model_key: str) -> int:
    """模型推荐并发数（config.json model_choices.<key>.workers），缺省 3。

    供流程层（mian.py）在未显式指定 --workers 时选择并发；batch_infer 内部
    已解析过模型配置，不会重复调用本函数（见 _resolve_workers）。
    """
    try:
        cfg = get_config(show_dialogs=False)
        return _resolve_workers(cfg.get("model_choices"), model_key, None)
    except Exception:
        return 3


def _server_help_text(exe: str) -> str:
    """获取 llama-server `--help` 输出文本（按 exe 缓存）；失败返回 ''。

    参数支持性/语法探测共用：一次探测、多次复用（不同构建的参数集不同）。
    """
    help_text = _ARG_HELP_CACHE.get(exe)
    if help_text is None:
        try:
            res = subprocess.run(
                [exe, "--help"],
                capture_output=True,
                text=True,
                timeout=15,
                errors="replace",
            )
            help_text = (res.stdout or "") + "\n" + (res.stderr or "")
        except Exception:
            help_text = ""
        _ARG_HELP_CACHE[exe] = help_text
    return help_text


def _server_supports_arg(exe: str, flag: str) -> bool:
    """探测 llama-server 构建是否支持某命令行参数（基于 `--help` 输出，按 exe 缓存）。

    不同版本/构建的参数集不同（如 llama13 构建无 ``--max-tokens``/``--ngram-size``/
    ``--window-size``），若把 DEFAULT_CONFIG 递归合并来的启动参数原样透传，
    进程会立即退出（``error: invalid argument: --max-tokens``，exit code 1）。
    探测失败时保守返回 True（不因探测故障阻断正常启动）。
    """
    help_text = _server_help_text(exe)
    if not help_text:
        return True
    # 逐行匹配 flag 作为独立 token（避免误命中 --spec-ngram-size-n 等相似参数）
    for line in help_text.splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        tokens = re.split(r"[\s,=]+", line)
        if flag in tokens:
            return True
    return False


def _server_flash_attn_style(exe: str) -> str | None:
    """探测构建的 --flash-attn 语法形式：'bare'（裸标志）| 'valued'（值形式）| None。

    llama.cpp 不同版本的 --flash-attn 语法不同：老构建是裸布尔标志
    （``-fa, --flash-attn``），新构建是带值标志（``-fa, --flash-attn [on|off|auto]``，
    默认 'auto'）。按错误语法透传会让 llama-server 直接
    ``error: invalid argument`` 退出——实测：裸标志传给新构建 → 打印 usage 后
    退出码 1 → "Server process exited unexpectedly"。值形式按帮助里的可选值
    文本（on|off|auto）识别；探测失败/帮助为空保守返回 None（不附加任何参数）。
    """
    help_text = _server_help_text(exe)
    if not help_text:
        return None
    lines = help_text.splitlines()
    for i, line in enumerate(lines):
        if "--flash-attn" not in line:
            continue
        # 值形式：帮助里会列出可选值（[on|off|auto] / <on|off|auto> / 'on','off','auto'）；
        # 顺带看后续 3 行（帮助描述换行时值说明可能折行）
        context = "\n".join(lines[i : i + 4])
        if re.search(r"on\s*\|\s*off|\bauto\b", context):
            return "valued"
        return "bare"
    return None


def _detect_gpu(exe: str):
    """检测 llama-server 是否支持 GPU 加速，返回 (backend, device) 或 (None, None)。

    通过 `llama-server --list-devices` 的输出判断（例如 "CUDA0: NVIDIA ..."）。
    backend 为 CUDA / Vulkan / ROCm / Metal 之一。
    """
    try:
        res = subprocess.run(
            [exe, "--list-devices"],
            capture_output=True,
            text=True,
            timeout=20,
            encoding="utf-8",
            errors="replace",
        )
        out = (res.stdout or "") + "\n" + (res.stderr or "")
    except Exception as e:
        print(f"GPU detection failed: {e}")
        return None, None
    m = re.search(r"\b(CUDA|Vulkan|ROCm|Metal)\d*:\s*([^\r\n]+)", out)
    if m:
        return m.group(1), m.group(2).strip()
    return None, None


def _model_id_matches(model_name: str, ids) -> bool:
    """比对 llama-server /v1/models 返回的模型 id 与配置中的模型名。

    llama-server 报告的 id 是启动时传入的完整路径（如 E:/model/qwen3.5/xxx.gguf），
    而 config 的 name 可能是相对路径（qwen3.5/xxx.gguf）——直接按路径比对恒不匹配，
    会把「同一模型已在运行」误判为 mismatch，导致 runserver 中止、矫正界面显示
    服务未启动（2026-08-08 修复）。先按规范化完整路径精确比对，再按 basename 兜底
    （大小写不敏感，兼容 Windows 路径分隔符）。
    """
    norm = lambda s: str(s).replace("\\", "/").strip().lower()
    if not model_name:
        return False
    target = norm(model_name)
    if not target:
        return False
    if any(norm(i) == target for i in ids):
        return True
    base = target.rsplit("/", 1)[-1]
    return bool(base) and any(norm(i).rsplit("/", 1)[-1] == base for i in ids)


def _probe_server(model_name: str) -> str:
    """探测 127.0.0.1:8080 上是否已有 llama-server，并比对模型。

    返回 'none'（无模型/端口不可达）| 'match'（已在运行且模型一致）|
    'mismatch'（端口被占用但模型不符或无法匹配）。
    防止启动时端口被旧实例占用导致新 Popen 绑定失败、health 却打到旧服务返回 ok，
    用错模型静默 OCR（S1）。
    """
    if _active_engine() == "vllm":
        return _vllm_module()._probe_server(model_name)
    try:
        resp = _SESSION.get("http://127.0.0.1:8080/health", timeout=2)
        if resp.status_code != 200:
            return "none"
    except requests.ConnectionError:
        return "none"
    except requests.RequestException:
        return "none"
    # 端口上有服务：通过 OpenAI 兼容 /v1/models 比对已加载模型
    try:
        r2 = _SESSION.get("http://127.0.0.1:8080/v1/models", timeout=2)
        if r2.status_code == 200:
            ids = [m.get("id") for m in r2.json().get("data", [])]
            if _model_id_matches(model_name, ids):
                return "match"
    except Exception:
        print("_probe_server failed")
    return "mismatch"


# 全部全局配置通过 configmanage 统一获取


def run(model_key: str = "HY"):
    if _active_engine() == "vllm":
        return _vllm_module().run(model_key=model_key)
    print("Running...")
    prompt = "按原文原格式输出"
    llama_server_cfg, models_dir_cfg, model_cfg, selected = _reload_config()
    if not check(llama_server_cfg, model_cfg, model_key):
        return
    if runserver(model_key):
        request(prompt, model_key)
    print("Request completed")


def check(llama: str, model_cfg: dict, model_key: str = "HY") -> bool:
    """检查 llama-server 可执行文件和模型文件是否存在。"""
    if _active_engine() == "vllm":
        return _vllm_module().check(llama, model_cfg, model_key=model_key)
    print("Checking...")
    # use provided llama if given, otherwise reload config
    if not llama:
        llama, models_dir, model_cfg, _ = _reload_config()
    # check llama-server executable
    if not os.path.isfile(llama):
        print(f"llama-server not found at: {llama}")
        return False
    model_info = model_cfg.get(model_key) if isinstance(model_cfg, dict) else None
    if model_info is None:
        print(f"Model key '{model_key}' not found in configuration")
        return False
    models_dir = models_dir if "models_dir" in locals() else os.path.dirname(llama)
    model_path = os.path.join(models_dir, model_info.get("name", ""))
    if not os.path.isfile(model_path):
        print(f"Model file not found: {model_path}")
        return False
    mmproj_path = os.path.join(models_dir, model_info.get("mmproj", ""))
    if not os.path.isfile(mmproj_path):
        print(f"MMProj file not found: {mmproj_path}")
        return False
    print("All checks passed")
    return True


# 使用 curl http://127.0.0.1:8080/health 检测服务器是否加载完成
# 加载失败或者未加载完成时
#   {"error":{"message":"Loading model","type":"unavailable_error","code":503}}
# 加载成功时
#   {"status":"ok"}
def runserver(model_key: str = "HY", with_mmproj: bool = True, parallel: int | None = None):
    """启动 llama-server 并等待模型加载完成。

    Args:
        model_key: model 字典中的模型键名，例如 "HY", "QWEN4" 等。
        with_mmproj: 是否附加 --mmproj 视觉投影器参数。矫正界面的文本深度校对
            （句子校正）不需要图像输入，以 False 启动可省去 mmproj（纯文本服务）。
            默认 True 保持 OCR 流程不变。
        parallel: 调用方已知的实际并发数（如 pdf_to_epub 的 workers）。传入时
            --parallel 取 min(配置值, parallel)——槽位不多于实际并发，避免 KV
            cache 按槽位预分配浪费显存（溢出到 CPU 反而拖慢单页）。
    """
    global _server_process

    if _active_engine() == "vllm":
        m = _vllm_module()
        ok = m.runserver(model_key, with_mmproj=with_mmproj, parallel=parallel)
        _server_process = m._server_process
        return ok

    # reload config to pick up latest paths
    llama_server_cfg, models_dir_cfg, model_cfg, selected = _reload_config()
    model_info = model_cfg.get(model_key) if isinstance(model_cfg, dict) else None
    if model_info is None:
        print(f"Model key '{model_key}' not found")
        return False
    model_path = os.path.join(models_dir_cfg, model_info.get("name", ""))
    mmproj_path = os.path.join(models_dir_cfg, model_info.get("mmproj", ""))
    if not os.path.exists(model_path):
        print(f"Model file not found: {model_path}")
        return False
    exe = llama_server_cfg
    model_name = model_info.get("name", model_key)

    # S1：启动前探测端口。旧 llama-server 存活时若直接 Popen，绑定 8080 会失败，
    # 而后续 health 检查会打到旧服务返回 ok —— 用错模型静默 OCR 且不报错。
    probe = _probe_server(model_name)
    if probe == "match":
        print(f"llama-server already running with model '{model_name}' — reusing")
        return True
    if probe == "mismatch":
        print(
            f"Port 8080 is occupied by a llama-server with a different model "
            f"(need '{model_name}'); stopping stale instance ..."
        )
        if _server_process is not None and _server_process.poll() is None:
            # 旧实例是本进程启动的：停掉后重启（避免静默用错模型）
            stopserver()
            time.sleep(0.5)
            if _probe_server(model_name) != "none":
                print(
                    "Port 8080 still occupied by an external process — abort to avoid "
                    "silent model mismatch; close it manually and retry"
                )
                return False
        else:
            print(
                "Port 8080 is held by an external process that ptoe cannot verify — "
                "abort to avoid silent model mismatch; close it manually and retry"
            )
            return False

    args = [
        exe,
        "-m",
        model_path,
    ]
    if with_mmproj:
        args += ["--mmproj", mmproj_path]

    # 启动参数从 config.json 的 llama_server_args 读取（值以字符串存储，原样传给
    # subprocess）。缺失/空值跳过该参数，回退到 llama-server 内置默认。
    # 服务器启动是一次性操作，此处多读一次 get_config() 无性能顾虑。
    cfg = get_config()
    sargs = cfg.get("llama_server_args", {}) or {}
    for key, flag in (
        ("host", "--host"),
        ("port", "--port"),
        ("temperature", "--temperature"),
        ("repeat_penalty", "--repeat-penalty"),
        ("cache_type_k", "--cache-type-k"),
        ("cache_type_v", "--cache-type-v"),
        ("log_verbosity", "--log-verbosity"),
    ):
        v = sargs.get(key)
        if v not in (None, ""):
            args += [flag, str(v)]

    # --parallel：llama.cpp 的 KV cache 总量 ≈ ctx × parallel（每个槽位独占
    # 一份完整上下文缓存），槽位多于实际并发只会浪费显存——KV 溢出到 CPU 时
    # 单页耗时反而大涨（多并行场景的主要瓶颈）。默认 4；流程调用方传入实际
    # 并发（pdf_to_epub 的 workers）时取 min(配置值, 实际并发)。
    try:
        par = int(sargs.get("parallel") or 4)
    except (TypeError, ValueError):
        par = 4
    if par < 1:
        par = 4
    if parallel is not None and parallel >= 1 and parallel < par:
        par = int(parallel)
    args += ["--parallel", str(par)]

    # Additional numeric startup parameters supported via config.json llama_server_args:
    # - max_tokens: integer (defaults to 8192) -> also controls per-request max_tokens used by HTTP calls
    # - ngram_size: integer (anti-repeat heuristic)
    # - window_size: integer (anti-repeat window)
    # Values are validated and out-of-range/invalid entries are ignored with a warning.
    try:
        mt = sargs.get("max_tokens")
        if mt not in (None, ""):
            mti = int(mt)
            if 128 <= mti <= 65536:
                global MAX_TOKENS
                MAX_TOKENS = mti
                # 请求级 max_tokens（HTTP payload）始终生效；仅当构建支持
                # --max-tokens 启动参数时才透传（否则进程启动即退出，见
                # _server_supports_arg）
                if _server_supports_arg(exe, "--max-tokens"):
                    args += ["--max-tokens", str(mti)]
                    print(f"Using max_tokens from config: {mti}")
                else:
                    print(
                        "llama-server 构建不支持 --max-tokens 启动参数，已跳过"
                        "（请求级 max_tokens 仍生效）"
                    )
            else:
                print(f"llama_server_args.max_tokens {mti} out of range (128-65536), ignoring")
    except Exception as e:
        print(f"Invalid llama_server_args.max_tokens: {e}")

    for name, flag, minv, maxv in (
        ("ngram_size", "--ngram-size", 1, 1024),
        ("window_size", "--window-size", 1, 4096),
    ):
        v = sargs.get(name)
        if v not in (None, ""):
            try:
                iv = int(v)
                if minv <= iv <= maxv:
                    if _server_supports_arg(exe, flag):
                        args += [flag, str(iv)]
                        print(f"Using {name} from config: {iv}")
                    else:
                        print(
                            f"llama-server 构建不支持 {flag} 启动参数，已跳过"
                        )
                else:
                    print(f"llama_server_args.{name} {iv} out of range ({minv}-{maxv}), ignoring")
            except Exception as e:
                print(f"Invalid llama_server_args.{name}: {e}")
    # GPU 后端：默认自动检测，可用则把全部层加载入显存（--n-gpu-layers 999）。
    # 若配置里显式给出 n_gpu_layers 键，则以其覆盖自动探测（跳过检测）。
    ngl = sargs.get("n_gpu_layers")
    if ngl not in (None, ""):
        args += ["--n-gpu-layers", str(ngl)]
        print(
            f"GPU acceleration: n_gpu_layers override from config (--n-gpu-layers {ngl})"
        )
    else:
        backend, device = _detect_gpu(exe)
        if backend:
            args += ["--n-gpu-layers", "999"]
            print(
                f"GPU acceleration: {backend} detected ({device}) - loading all layers to VRAM (--n-gpu-layers 999)"
            )
            # Flash Attention：CUDA/Vulkan 下长上下文解码显著加速（视觉页含
            # 数千图像 token，收益最大）。语法按构建自适应（见
            # _server_flash_attn_style）：老构建附加裸 --flash-attn；新构建
            # （值形式 [on|off|auto]，默认 auto）不传参数——CUDA 支持时自动
            # 开启、不支持时安全回退，避免裸标志导致启动失败（llama13 实测）。
            # llama_server_args.flash_attn：
            #   "0"/false/no/off → 禁用；
            #   "1"/true/yes/on  → 强制开启（按构建语法传值/传裸标志）；
            #   缺省             → 自动（新构建 auto / 老构建裸标志）。
            fa = str(sargs.get("flash_attn") or "").strip().lower()
            if backend in ("CUDA", "Vulkan") and fa not in ("0", "false", "no", "off"):
                style = _server_flash_attn_style(exe)
                if fa in ("1", "true", "yes", "on") and style == "valued":
                    args += ["--flash-attn", "on"]
                    print("Flash Attention: enabled (--flash-attn on)")
                elif style == "bare":
                    args += ["--flash-attn"]
                    print("Flash Attention: enabled (--flash-attn)")
                elif style == "valued":
                    print(
                        "Flash Attention: auto（构建默认，CUDA 支持时自动启用；"
                        "llama_server_args.flash_attn=0 可禁用，=1 强制开启）"
                    )
        else:
            print("GPU acceleration: no GPU backend detected - running on CPU")

    print(f"Starting server: {' '.join(args)}")

    try:
        _server_process = subprocess.Popen(args)
    except FileNotFoundError:
        print(f"llama-server not found at: {exe}")
        return False

    # 轮询 health 接口等待模型加载，超时见 _HEALTH_TIMEOUT（秒）
    timeout = _HEALTH_TIMEOUT
    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout:
        if _server_process.poll() is not None:
            print("Server process exited unexpectedly")
            stopserver()
            return False
        try:
            response = _SESSION.get("http://127.0.0.1:8080/health", timeout=5)
            if response.status_code == 200:
                print("Server loaded successfully")
                return True
            # 503（Loading model）等非 200：服务未就绪，稍候重试。
            # 不 sleep 会忙轮询打满正在加载模型的进程，拖慢启动（2026-08-08）。
            time.sleep(1)
        except requests.ConnectionError:
            # 服务器尚未就绪，继续等待
            time.sleep(1)
        except requests.RequestException as e:
            print(f"Health check error: {e}")
            time.sleep(1)

    print("Server startup timed out")
    stopserver()
    return False


def _server_port() -> str:
    """当前 llama 引擎的监听端口（llama_server_args.port，默认 8080）。"""
    try:
        cfg = get_config(show_dialogs=False)
        return str((cfg.get("llama_server_args") or {}).get("port") or "8080")
    except Exception:
        return "8080"


def _kill_port_owner(port) -> bool:
    """杀掉占用指定端口的进程（Windows：netstat -ano + taskkill /T /F）。

    用于 stopserver 兜底：llama-server 可能由上次运行/其他进程启动，
    _server_process 不可用时仍能真正释放端口（2026-08-13）。
    非 Windows 或命令失败时返回 False（不抛异常）。
    """
    import subprocess as sp

    port = str(port)
    try:
        out = sp.check_output(
            ["netstat", "-ano", "-p", "tcp"],
            text=True,
            errors="replace",
            timeout=10,
        )
    except Exception:
        return False
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if (
            len(parts) >= 5
            and parts[1].rsplit(":", 1)[-1] == port
            and parts[3].upper() == "LISTENING"
        ):
            pids.add(parts[4])
    if not pids:
        return False
    killed = False
    for pid in pids:
        try:
            sp.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
            killed = True
        except Exception:
            continue
    return killed


def stopserver():
    """停止正在运行的推理服务进程（llama-server / vLLM）。

    先停本进程启动的实例（_server_process）；若端口上仍有服务（上次运行遗留
    或外部启动、模型不符），兜底按配置端口杀进程，确保端口真正释放
    （2026-08-13：矫正界面「停止服务」与 CLI stop 命令都能关掉遗留实例）。
    """
    global _server_process

    if _active_engine() == "vllm":
        m = _vllm_module()
        ok = m.stopserver()
        _server_process = m._server_process
        return ok

    if _server_process is not None and _server_process.poll() is None:
        print("Server stopping...")
        _server_process.terminate()
        try:
            _server_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _server_process.kill()
            _server_process.wait()
        _server_process = None
        print("Server stopped successfully")
        return True

    _server_process = None
    # 本进程无跟踪实例：端口上可能还有上次运行遗留/外部启动的 llama-server
    # （矫正界面模型不符时「停止服务」无效的根因），按配置端口兜底杀掉。
    port = _server_port()
    if port and _kill_port_owner(port):
        print(f"Server stopped successfully (port {port})")
    else:
        print("No server process is running")
    return True


def request(
    prompt: str = "Hello",
    model_key: str = "HY",
    thinking: bool = False,
    append_ocr_instruction: bool = True,
):
    """向 llama-server 发送推理请求。返回 {'result': ..., 'error': ...}
    If thinking is False, appends the '按原文原格式输出' instruction
    (unless append_ocr_instruction=False — e.g. JSON-output proofreading prompts
     where the OCR suffix would corrupt the expected response format).
    """
    if _active_engine() == "vllm":
        return _vllm_module().request(
            prompt,
            model_key=model_key,
            thinking=thinking,
            append_ocr_instruction=append_ocr_instruction,
        )
    try:
        if not thinking and append_ocr_instruction:
            prompt = f"{prompt}\n按原文原格式输出"
        llama_server_cfg, models_dir_cfg, model_cfg, selected = _reload_config()
        model_name = (
            model_cfg.get(model_key, {}).get("name", model_key)
            if isinstance(model_cfg, dict)
            else model_key
        )
        url = "http://127.0.0.1:8080/v1/chat/completions"
        data = {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "stop": ["\n\n"],
            "max_tokens": MAX_TOKENS,
            "chat_template_kwargs": {"enable_thinking": thinking},
        }
        headers = {"Content-Type": "application/json"}
        resp = _SESSION.post(url, json=data, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        result = resp.json()
        if result.get("choices"):
            return {"result": result["choices"][0]["message"]["content"], "error": None}
        return {"result": None, "error": f"No choices in response: {result}"}
    except Exception as e:
        print(f"[request] Request failed: {e}")
        return {"result": None, "error": str(e)}


def _encode_img_local(img_path):
    try:
        if not os.path.exists(str(img_path)):
            return None
        with open(str(img_path), "rb") as f:
            data = f.read()
        return base64.b64encode(data).decode("utf-8")
    except Exception:
        return None


def batch_infer(
    images: list,
    prompts: list,
    model_key: str = "HY",
    max_workers: int | None = None,
    thinking: bool = False,
    timeout: int = REQUEST_TIMEOUT,
    on_progress=None,
    on_result=None,
):
    """
    并发批量图片识别。return: List[dict], 每元素结构{'img': ..., 'result': ..., 'error': ...}
    支持 thinking 透传。任一图片异常都不抛出，但会返回该图片的 error
    on_progress: 可选回调 on_progress(done, total)，每完成一张图片时调用
    on_result: 可选回调 on_result(result)，每完成一张图片时调用，result 与
    返回值元素同构（{'img': ..., 'result': ..., 'error': ...}）——断点续传用它
    按页持久化结果，中断后已完成的页不丢失。

    性能：模型名/引擎/并发在批次开始前解析一次并传入各请求（避免每页都
    get_config() 读文件+抢锁），HTTP 复用 _SESSION 连接（keep-alive）。
    max_workers=None 时取模型推荐并发（model_choices.<key>.workers，设置页
    模型管理可调），未配置则 3：视觉模型每张图数千图像 token，并发过高会让
    多槽位的 KV 缓存溢出到 CPU，单张耗时反而大涨（8GB 显存 + BF16 模型尤其
    明显）。显存充足时可手动调大（如 --workers 6）。
    """
    engine = _active_engine()
    global _BATCH_ENGINE
    _prev_batch = _BATCH_ENGINE
    _BATCH_ENGINE = engine
    try:
        if engine == "vllm":
            return _vllm_module().batch_infer(
                images,
                prompts,
                model_key=model_key,
                max_workers=max_workers,
                thinking=thinking,
                timeout=timeout,
                on_progress=on_progress,
                on_result=on_result,
            )
        return _batch_infer_impl(
            images,
            prompts,
            model_key=model_key,
            max_workers=max_workers,
            thinking=thinking,
            timeout=timeout,
            on_progress=on_progress,
            on_result=on_result,
        )
    finally:
        _BATCH_ENGINE = _prev_batch


def _batch_infer_impl(
    images: list,
    prompts: list,
    model_key: str,
    max_workers: int | None,
    thinking: bool,
    timeout: int,
    on_progress,
    on_result,
):

    def infer_one(img, prompt):
        t0 = time.perf_counter()
        try:
            r = _request_image_new(
                prompt,
                img,
                model_key,
                thinking=thinking,
                timeout=timeout,
                model_name=model_name,
            )
        finally:
            # P5 (optimized): 不再逐页清理 ImageItem 缓存。
            # 逐页 clear() 会导致 N 页书的每张图片被读取/编码 N 次（每页都重新读盘）。
            # 改为批次结束后统一清理（见下方 finally 块），单次编码复用全批次。
            pass
        dt = time.perf_counter() - t0
        name = getattr(img, "path", img)
        if r.get("error"):
            print(f"[OCR] {name} FAILED after {dt:.1f}s: {r['error']}")
        else:
            chars = len(r.get("result") or "")
            print(f"[OCR] {name} done in {dt:.1f}s ({chars} chars)")
        return {"img": img, **r}

    if not images:
        return []

    # 批次内配置只解析一次：模型名/推荐并发不会中途变化，避免每页重复读
    # config.json（get_config 会抢全局锁、做路径校验，路径无效时还会在桌面
    # 弹 tkinter 对话框）
    model_name: str | None = None
    try:
        _llama_cfg, _models_cfg, _model_cfg, _selected = _reload_config()
        model_name = (
            _model_cfg.get(model_key, {}).get("name", model_key)
            if isinstance(_model_cfg, dict)
            else model_key
        )
        max_workers = _resolve_workers(_model_cfg, model_key, max_workers)
    except Exception as e:
        # S3：配置解析失败时整批返回明确错误，而不是每页都重新解析
        # （逐页兜底会导致每页都可能弹 tkinter 对话框、重复读文件）。
        msg = f"config resolution failed: {e}"
        print(f"[batch_infer] {msg}")
        return [{"img": img, "result": None, "error": msg} for img in images]
    # prompts 可为单值/同长序列
    if isinstance(prompts, str):
        prompts = [prompts] * len(images)
    if len(prompts) == 1:
        prompts = prompts * len(images)
    assert len(images) == len(prompts), "images/prompts必须等长"
    results = []
    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_map = {
            pool.submit(infer_one, img, prompts[i]): i for i, img in enumerate(images)
        }
        done = 0
        total = len(images)
        for fut in as_completed(fut_map):
            idx = fut_map[fut]
            try:
                res = fut.result()
            except Exception as e:
                print(f"[batch_infer] Batch image failed: {e}")
                res = {"img": images[idx], "result": None, "error": str(e)}
            results.append(res)
            done += 1
            if on_progress is not None:
                on_progress(done, total)
            if on_result is not None:
                on_result(res)

    # 批次结束后统一清理 ImageItem 缓存（避免逐页 clear 导致重复读盘/编码）
    for img in images:
        cleaner = getattr(img, "clear", None)
        if callable(cleaner):
            try:
                cleaner()
            except Exception:
                pass

    return results


def _request_image_new(
    prompt: str,
    img,
    model_key: str = "HY",
    thinking: bool = False,
    img_is_base64: bool = False,
    timeout: int = REQUEST_TIMEOUT,
    model_name: str | None = None,
    img_bytes: bytes | None = None,
):
    """New request_image implementation (duck-typed) that wraps existing behavior.
    This function is appended and then assigned to the public name to safely
    override previous definitions without risky in-place edits.

    model_name：批量识别时由 batch_infer 一次性从配置解析并传入，避免每页
    都调 get_config()（读/写 config.json、抢锁、路径无效时弹 tkinter 对话框）。
    为 None 时按旧行为自行解析（单张/外部调用兜底）。
    img_bytes：可选内存图片字节（如 _full_bytes 返回的原始数据），提供时跳过
    磁盘临时文件，直接 base64 编码后发送，减少 /api/reocr 等场景的 I/O 开销。
    """
    if _active_engine() == "vllm":
        return _vllm_module()._request_image_new(
            prompt,
            img,
            model_key=model_key,
            thinking=thinking,
            img_is_base64=img_is_base64,
            timeout=timeout,
            model_name=model_name,
            img_bytes=img_bytes,
        )
    try:
        _mime = "image/png"
        # Use in-memory bytes if provided (skip temp file disk I/O)
        if img_bytes is not None:
            img_base64 = base64.b64encode(img_bytes).decode("ascii")
            _mime = "image/jpeg"
        else:
            img_base64 = None
            if img_is_base64 and isinstance(img, str):
                img_base64 = img
            elif hasattr(img, "get_base64") and callable(img.get_base64):
                img_base64 = img.get_base64()
            else:
                img_base64 = _encode_img_local(img)

        if img_base64 is None:
            return {
                "result": None,
                "error": f"Image not found or encoding failed: {img}",
            }

        if img_bytes is None:
            # guess the media type from the image path so jpg images are not sent as png
            _img_path = (
                img
                if (isinstance(img, str) and not img_is_base64)
                else getattr(img, "path", "")
            )
            _mime = (
                "image/jpeg"
                if str(_img_path).lower().endswith((".jpg", ".jpeg"))
                else "image/png"
            )

        prompt_ = f"{prompt}\n按原文原格式输出" if not thinking else prompt
        if model_name is None:
            # 兜底：直接调用（非 batch）时才逐次解析配置
            llama_server_cfg, models_dir_cfg, model_cfg, selected = _reload_config()
            model_name = (
                model_cfg.get(model_key, {}).get("name", model_key)
                if isinstance(model_cfg, dict)
                else model_key
            )
        url = "http://127.0.0.1:8080/v1/chat/completions"
        data = {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{_mime};base64,{img_base64}"},
                        },
                    ],
                }
            ],
            "stream": False,
            # 生成上限兜底：Qwen3 系思考链默认开启，跑飞时单请求可达数万 token
            # （实测 4 万+，一张图拖 5 分钟+），4096 内正常页面绝不会触顶
            "max_tokens": MAX_TOKENS,
            # Qwen3.5 等模型默认开启隐藏思考链，OCR 时大部分时间花在推理而非输出
            # （实测 100dpi 单页 2965 tokens≈14.9s → 关思考后 392 tokens≈2.1s）。
            # enable_thinking 显式跟随 thinking 参数；非 Qwen 模板会忽略该键，无副作用
            "chat_template_kwargs": {"enable_thinking": thinking},
        }
        headers = {"Content-Type": "application/json"}
        # 复用会话连接（keep-alive），避免每页新建 TCP 连接
        resp = _SESSION.post(url, json=data, headers=headers, timeout=timeout)
        resp.raise_for_status()
        result = resp.json()
        if result.get("choices"):
            choice = result["choices"][0]
            # 提示词即使加了完整性要求，极长页面仍可能触顶 max_tokens 被截断
            # （正常页 400-1000 tokens，绝不该到 4096）——打印警告便于发现内容丢失
            if choice.get("finish_reason") == "length":
                print(
                    f"[request_image] WARNING: {img} hit max_tokens={MAX_TOKENS} "
                    f"(finish_reason=length) — 输出可能被截断，请检查该页内容"
                )
            return {"result": choice["message"]["content"], "error": None}
        return {"result": None, "error": f"No choices in response: {result}"}
    except Exception as e:
        print(f"[request_image] Request failed for {img}: {e}")
        return {"result": None, "error": str(e)}


# override public name
request_image = _request_image_new
