import base64
import os
import re
import subprocess
import time
from typing import Optional

import requests

from configmanage import get_config

_server_process: subprocess.Popen | None = None

# 复用 HTTP 连接（keep-alive）：多页 OCR 时避免每页都新建 TCP 连接/握手
_SESSION = requests.Session()

# 单次推理请求超时（秒）。300dpi 页面会被编码成约 8700 个图像 token（200dpi 约 4600），
# 4 并发时图片编码+生成可能超过 1 分钟，默认 60s 会误杀正常请求。
REQUEST_TIMEOUT = 600

# 单页 OCR 输出上限（token）。一页识别文本约 400-900 tokens（≈2800 汉字），
# 4096 足以覆盖任何正常页面；防止 Qwen3 系模型思考链跑飞/死循环时无限生成
# （实测跑飞单请求可达 4 万+ tokens），也防止异常页面拖垮整个批次。
MAX_TOKENS = 4096

# 统一 OCR 提示词：强调逐行完整、逐字输出，缓解 0.8B 模型跳字/漏行
# （实测最差页字符数 +20%，无速度代价）。所有调用方共用此常量，禁止各自
# 内联自定义 OCR 提示词；llamamanage 还会在非 thinking 模式下追加
# "\n按原文原格式输出" 后缀。
OCR_PROMPT = "请逐行完整识别图片中的全部文字，逐字输出，不得遗漏任何内容、不得省略、不得总结、不得翻译"


def _reload_config():
    cfg = get_config()
    return (
        cfg.get("llama_server"),
        cfg.get("models_dir"),
        cfg.get("model_choices"),
        cfg.get("selected_model"),
    )


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


def _probe_server(model_name: str) -> str:
    """探测 127.0.0.1:8080 上是否已有 llama-server，并比对模型。

    返回 'none'（端口空闲/不可达）| 'match'（已在运行且模型一致）|
    'mismatch'（端口被占用但模型不符或无法确认）。
    防止旧实例存活时新 Popen 绑定失败、health 却打到旧服务返回 ok，
    导致用错模型静默 OCR（S1）。
    """
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
            if model_name in ids:
                return "match"
    except Exception:
        pass
    return "mismatch"


# 全部全局配置通过 configmanage 统一获取


def run(model_key: str = "HY"):
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
def runserver(model_key: str = "HY"):
    """启动 llama-server 并等待模型加载完成。

    Args:
        model_key: model 字典中的模型键名，例如 "HY", "QWEN4" 等。
    """
    global _server_process

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
        "--mmproj",
        mmproj_path,
    ]

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
        ("parallel", "--parallel"),
        ("cache_type_k", "--cache-type-k"),
        ("cache_type_v", "--cache-type-v"),
        ("log_verbosity", "--log-verbosity"),
    ):
        v = sargs.get(key)
        if v not in (None, ""):
            args += [flag, str(v)]

    # GPU 后端：默认自动检测，可用则把全部层加载入显存（--n-gpu-layers 999）。
    # 若配置里显式给出 n_gpu_layers 键，则以其覆盖自动探测（跳过检测）。
    ngl = sargs.get("n_gpu_layers")
    if ngl not in (None, ""):
        args += ["--n-gpu-layers", str(ngl)]
        print(f"GPU acceleration: n_gpu_layers override from config (--n-gpu-layers {ngl})")
    else:
        backend, device = _detect_gpu(exe)
        if backend:
            args += ["--n-gpu-layers", "999"]
            print(
                f"GPU acceleration: {backend} detected ({device}) - loading all layers to VRAM (--n-gpu-layers 999)"
            )
        else:
            print("GPU acceleration: no GPU backend detected - running on CPU")

    print(f"Starting server: {' '.join(args)}")

    try:
        _server_process = subprocess.Popen(args)
    except FileNotFoundError:
        print(f"llama-server not found at: {exe}")
        return False

    # 轮询 health 接口等待模型加载，超时 120 秒
    timeout = 120
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
        except requests.ConnectionError:
            # 服务器尚未就绪，继续等待
            time.sleep(1)
        except requests.RequestException as e:
            print(f"Health check error: {e}")
            time.sleep(1)

    print("Server startup timed out")
    stopserver()
    return False


def stopserver():
    """优雅停止正在运行的 llama-server 进程。"""
    global _server_process

    if _server_process is None or _server_process.poll() is not None:
        print("No server process is running")
        _server_process = None
        return True

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


def request(prompt: str = "Hello", model_key: str = "HY", thinking: bool = False):
    """向 llama-server 发送推理请求。返回 {'result': ..., 'error': ...}
    If thinking is False, appends the '按原文原格式输出' instruction.
    """
    try:
        if not thinking:
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
        if "choices" in result and result["choices"]:
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


def request_image(
    prompt: str,
    img,
    model_key: str = "HY",
    thinking: bool = False,
    img_is_base64: bool = False,
):
    """对单张图片进行识别。
    img can be a file path, a base64 string (set img_is_base64=True), or an ImageItem instance.
    Returns {'result':..., 'error':...}.
    """
    try:
        # attempt to import ImageItem type
        try:
            from pdfmanage import ImageItem
        except Exception:
            ImageItem = None

        img_base64 = None
        if img_is_base64 and isinstance(img, str):
            img_base64 = img
        elif ImageItem is not None and isinstance(img, ImageItem):
            img_base64 = img.get_base64()
        else:
            # assume path-like
            img_base64 = _encode_img_local(img)

        if img_base64 is None:
            return {
                "result": None,
                "error": f"Image not found or encoding failed: {img}",
            }

        prompt_ = f"{prompt}\n按原文原格式输出" if not thinking else prompt
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
                            "image_url": {"url": f"data:image/png;base64,{img_base64}"},
                        },
                    ],
                }
            ],
            "stream": False,
            "stop": ["\n\n"],
        }
        headers = {"Content-Type": "application/json"}
        resp = _SESSION.post(url, json=data, headers=headers, timeout=60)
        resp.raise_for_status()
        result = resp.json()
        if "choices" in result and result["choices"]:
            return {"result": result["choices"][0]["message"]["content"], "error": None}
        return {"result": None, "error": f"No choices in response: {result}"}
    except Exception as e:
        print(f"[request_image] Request failed for {img}: {e}")
        return {"result": None, "error": str(e)}


def batch_infer(
    images: list,
    prompts: list,
    model_key: str = "HY",
    max_workers: int = 3,
    thinking: bool = False,
    timeout: int = REQUEST_TIMEOUT,
    on_progress=None,
):
    """
    并发批量图片识别。return: List[dict], 每元素结构{'img': ..., 'result': ..., 'error': ...}
    支持 thinking 透传。任一图片异常都不抛出，但会返回该图片的 error
    on_progress: 可选回调 on_progress(done, total)，每完成一张图片时调用

    性能：模型名/配置在批次开始前解析一次并传入各请求（避免每页都
    get_config() 读文件+抢锁），HTTP 复用 _SESSION 连接（keep-alive）。
    默认 max_workers=3：视觉模型每张图数千图像 token，并发过高会让多槽位
    的 KV 缓存溢出到 CPU，单张耗时反而大涨（8GB 显存 + BF16 模型尤其明显）。
    显存充足时可手动调大（如 --workers 6）。
    """

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
            # P5：请求结束后清理 ImageItem 的 base64 临时文件/内存缓存，
            # 避免整批图片的 base64 全量驻留磁盘/内存。路径模式下无副作用。
            cleaner = getattr(img, "clear", None)
            if callable(cleaner):
                try:
                    cleaner()
                except Exception:
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

    # 批次内配置只解析一次：模型名不会中途变化，避免每页重复读 config.json
    # （get_config 会抢全局锁、做路径校验，路径无效时还会在桌面弹 tkinter 对话框）
    model_name: Optional[str] = None
    try:
        _llama_cfg, _models_cfg, _model_cfg, _selected = _reload_config()
        model_name = (
            _model_cfg.get(model_key, {}).get("name", model_key)
            if isinstance(_model_cfg, dict)
            else model_key
        )
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
                results.append(res)
            except Exception as e:
                print(f"[batch_infer] Batch image failed: {e}")
                results.append({"img": images[idx], "result": None, "error": str(e)})
            done += 1
            if on_progress is not None:
                on_progress(done, total)
    return results


def _request_image_new(
    prompt: str,
    img,
    model_key: str = "HY",
    thinking: bool = False,
    img_is_base64: bool = False,
    timeout: int = REQUEST_TIMEOUT,
    model_name: Optional[str] = None,
):
    """New request_image implementation (duck-typed) that wraps existing behavior.
    This function is appended and then assigned to the public name to safely
    override previous definitions without risky in-place edits.

    model_name：批量识别时由 batch_infer 一次性从配置解析并传入，避免每页
    都调 get_config()（读/写 config.json、抢锁、路径无效时弹 tkinter 对话框）。
    为 None 时按旧行为自行解析（单张/外部调用兜底）。
    """
    try:
        img_base64 = None
        if img_is_base64 and isinstance(img, str):
            img_base64 = img
        elif hasattr(img, "get_base64") and callable(getattr(img, "get_base64")):
            img_base64 = img.get_base64()
        else:
            img_base64 = _encode_img_local(img)

        if img_base64 is None:
            return {
                "result": None,
                "error": f"Image not found or encoding failed: {img}",
            }

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
        if "choices" in result and result["choices"]:
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
