import base64
import os
import re
import subprocess
import time

import requests

from configmanage import get_config
_server_process: subprocess.Popen | None = None

# 单次推理请求超时（秒）。300dpi 页面会被编码成约 8700 个图像 token（200dpi 约 4600），
# 4 并发时图片编码+生成可能超过 1 分钟，默认 60s 会误杀正常请求。
REQUEST_TIMEOUT = 600

def _reload_config():
    cfg = get_config()
    return cfg.get('llama_server'), cfg.get('models_dir'), cfg.get('model_choices'), cfg.get('selected_model')


def _detect_gpu(exe: str):
    """检测 llama-server 是否支持 GPU 加速，返回 (backend, device) 或 (None, None)。

    通过 `llama-server --list-devices` 的输出判断（例如 "CUDA0: NVIDIA ..."）。
    backend 为 CUDA / Vulkan / ROCm / Metal 之一。
    """
    try:
        res = subprocess.run(
            [exe, "--list-devices"],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
        )
        out = (res.stdout or "") + "\n" + (res.stderr or "")
    except Exception as e:
        print(f"GPU detection failed: {e}")
        return None, None
    m = re.search(r"\b(CUDA|Vulkan|ROCm|Metal)\d*:\s*([^\r\n]+)", out)
    if m:
        return m.group(1), m.group(2).strip()
    return None, None


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
    models_dir = models_dir if 'models_dir' in locals() else os.path.dirname(llama)
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


    args = [
        exe,
        "-m",
        model_path,
        "--mmproj",
        mmproj_path,
        "--host",
        "127.0.0.1",
        "--port",
        "8080",
        "--temperature",
        "0",
        "--repeat-penalty",
        "1.1",
        "--parallel",
        "4",
        # KV 缓存量化：8GB 显存装下 BF16 模型后仅剩约 600MB，
        # 4 slot 的 f16 KV cache 会溢出到 CPU，导致生成阶段跌到 1-5 t/s。
        # q8_0 量化后 KV 体积减半，可留在显存，生成速度恢复 50+ t/s。
        "--cache-type-k",
        "q8_0",
        "--cache-type-v",
        "q8_0",
    ]

    # 检测 GPU 后端：若可用则默认把全部层加载入显存（--n-gpu-layers 999）
    backend, device = _detect_gpu(exe)
    if backend:
        args += ["--n-gpu-layers", "999"]
        print(f"GPU acceleration: {backend} detected ({device}) - loading all layers to VRAM (--n-gpu-layers 999)")
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
            response = requests.get("http://127.0.0.1:8080/health", timeout=5)
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
        model_name = model_cfg.get(model_key, {}).get("name", model_key) if isinstance(model_cfg, dict) else model_key
        url = "http://127.0.0.1:8080/v1/chat/completions"
        data = {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "stop": ["\n\n"],
        }
        headers = {"Content-Type": "application/json"}
        resp = requests.post(url, json=data, headers=headers, timeout=REQUEST_TIMEOUT)
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
        with open(str(img_path), 'rb') as f:
            data = f.read()
        return base64.b64encode(data).decode('utf-8')
    except Exception:
        return None

def request_image(prompt: str, img, model_key: str = "HY", thinking: bool = False, img_is_base64: bool = False):
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
            return {"result": None, "error": f"Image not found or encoding failed: {img}"}

        prompt_ = f"{prompt}\n按原文原格式输出" if not thinking else prompt
        llama_server_cfg, models_dir_cfg, model_cfg, selected = _reload_config()
        model_name = model_cfg.get(model_key, {}).get("name", model_key) if isinstance(model_cfg, dict) else model_key
        url = "http://127.0.0.1:8080/v1/chat/completions"
        data = {
            "model": model_name,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt_},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_base64}"}},
            ]}],
            "stream": False,
            "stop": ["\n\n"],
        }
        headers = {"Content-Type": "application/json"}
        resp = requests.post(url, json=data, headers=headers, timeout=60)
        resp.raise_for_status()
        result = resp.json()
        if "choices" in result and result["choices"]:
            return {"result": result["choices"][0]["message"]["content"], "error": None}
        return {"result": None, "error": f"No choices in response: {result}"}
    except Exception as e:
        print(f"[request_image] Request failed for {img}: {e}")
        return {"result": None, "error": str(e)}


def batch_infer(images: list, prompts: list, model_key: str = "HY", max_workers: int = 3, thinking: bool = False, timeout: int = REQUEST_TIMEOUT, on_progress=None):
    """
    并发批量图片识别。return: List[dict], 每元素结构{'img': ..., 'result': ..., 'error': ...}
    支持 thinking 透传。任一图片异常都不抛出，但会返回该图片的 error
    on_progress: 可选回调 on_progress(done, total)，每完成一张图片时调用
    """
    def infer_one(img, prompt):
        t0 = time.perf_counter()
        r = _request_image_new(prompt, img, model_key, thinking=thinking, timeout=timeout)
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
    # prompts 可为单值/同长序列
    if isinstance(prompts, str):
        prompts = [prompts] * len(images)
    if len(prompts) == 1:
        prompts = prompts * len(images)
    assert len(images) == len(prompts), "images/prompts必须等长"
    results = []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_map = {pool.submit(infer_one, img, prompts[i]): i for i, img in enumerate(images)}
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



def _request_image_new(prompt: str, img, model_key: str = "HY", thinking: bool = False, img_is_base64: bool = False, timeout: int = REQUEST_TIMEOUT):
    """New request_image implementation (duck-typed) that wraps existing behavior.
    This function is appended and then assigned to the public name to safely
    override previous definitions without risky in-place edits.
    """
    try:
        img_base64 = None
        if img_is_base64 and isinstance(img, str):
            img_base64 = img
        elif hasattr(img, 'get_base64') and callable(getattr(img, 'get_base64')):
            img_base64 = img.get_base64()
        else:
            img_base64 = _encode_img_local(img)

        if img_base64 is None:
            return {"result": None, "error": f"Image not found or encoding failed: {img}"}

        # guess the media type from the image path so jpg images are not sent as png
        _img_path = img if (isinstance(img, str) and not img_is_base64) else getattr(img, 'path', '')
        _mime = 'image/jpeg' if str(_img_path).lower().endswith(('.jpg', '.jpeg')) else 'image/png'

        prompt_ = f"{prompt}\n按原文原格式输出" if not thinking else prompt
        llama_server_cfg, models_dir_cfg, model_cfg, selected = _reload_config()
        model_name = model_cfg.get(model_key, {}).get("name", model_key) if isinstance(model_cfg, dict) else model_key
        url = "http://127.0.0.1:8080/v1/chat/completions"
        data = {
            "model": model_name,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt_},
                {"type": "image_url", "image_url": {"url": f"data:{_mime};base64,{img_base64}"}},
            ]}],
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        resp = requests.post(url, json=data, headers=headers, timeout=timeout)
        resp.raise_for_status()
        result = resp.json()
        if "choices" in result and result["choices"]:
            return {"result": result["choices"][0]["message"]["content"], "error": None}
        return {"result": None, "error": f"No choices in response: {result}"}
    except Exception as e:
        print(f"[request_image] Request failed for {img}: {e}")
        return {"result": None, "error": str(e)}

# override public name
request_image = _request_image_new

