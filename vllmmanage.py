"""vLLM-Omni 推理引擎适配层（与 llamamanage 同构 API）。

vLLM-Omni（https://github.com/vllm-project/vllm-omni）是 vLLM 的多模态扩展，
官方仅支持 Linux（Windows 用户可在 WSL2/远程主机手动启动服务后以「连接模式」
使用本模块）。本模块实现与 llamamanage 相同的对外 API（runserver/stopserver/
_probe_server/request/_request_image_new/batch_infer/check/run），由
llamamanage 按 config.json 的 engine 键（"llama"|"vllm"）分发调用。

两种运行模式：
- 启动模式：config.json 配置 vllm_server 可执行文件路径后，runserver 自动
  拉起 `vllm serve <model> --omni ...` 并等待模型就绪（/v1/models 轮询）。
- 连接模式：vllm_server 为空时仅探测/连接已在运行的服务（WSL2/远程手动启动）。

与 llama.cpp 引擎的差异：
- 客户端 URL 从 vllm_server_args.host/port 构建（默认 127.0.0.1:8000），
  不硬编码 8080。
- 就绪探测用 /v1/models（vLLM 的 /health 仅存活探测，模型加载中也返回 200）。
- 多模态请求体附加 "modalities": ["text"]，跳过音频生成阶段（OCR 更快）。
- 模型名 = HF 模型 id 或本地目录（GGUF 不受 vLLM 支持）；mmproj 概念不存在，
  with_mmproj 参数仅为与 llamamanage 签名一致而保留。
"""

import base64
import os
import shlex
import subprocess
import time
from typing import Optional

import requests

from configmanage import get_config
from llamamanage import (
    _SESSION,
    _kill_port_owner,
    _model_id_matches,
    _resolve_workers,
    MAX_TOKENS,
    REQUEST_TIMEOUT,
)

# vLLM 就绪轮询超时（秒）：大模型从磁盘加载到显存可能超过 2 分钟
_HEALTH_TIMEOUT = 300

_server_process: subprocess.Popen | None = None


def _vll_args():
    """读取 vllm 相关配置。返回 (vllm_server, vllm_server_args, models_dir, model_choices)。"""
    cfg = get_config(show_dialogs=False)
    return (
        cfg.get("vllm_server") or "",
        cfg.get("vllm_server_args") or {},
        cfg.get("models_dir") or "",
        cfg.get("model_choices") or {},
    )


def _base_url(sargs: dict) -> str:
    """由 vllm_server_args 构建客户端 URL（默认 127.0.0.1:8000）。"""
    host = str(sargs.get("host") or "127.0.0.1")
    port = str(sargs.get("port") or "8000")
    return f"http://{host}:{port}"


def _resolve_model(model_key: str):
    """返回 (model_name, model_arg)。

    model_name：用于 /v1/models 比对（HF id 或本地路径）。
    model_arg：传给 `vllm serve` 的模型参数（本地路径或 HF id）。
    """
    _, _, models_dir, model_cfg = _vll_args()
    info = model_cfg.get(model_key) if isinstance(model_cfg, dict) else None
    if info is None:
        return None, None
    name = str(info.get("name") or model_key)
    # 本地路径：绝对路径或 models_dir 下存在；否则视为 HF 模型 id
    if os.path.isabs(name) or os.path.exists(name):
        return name, name
    local = os.path.join(models_dir, name) if models_dir else name
    if os.path.exists(local):
        return local, local
    return name, name


def _probe_server(model_name: str) -> str:
    """探测 vLLM 服务并比对模型。返回 'none'|'match'|'mismatch'。

    vLLM 的 /health 仅存活探测（模型加载中也返回 200），模型是否就绪看
    /v1/models（加载中 503，就绪后 200 且 data[].id 含目标模型）。
    """
    _, sargs, _, _ = _vll_args()
    base = _base_url(sargs)
    try:
        resp = _SESSION.get(f"{base}/health", timeout=2)
        if resp.status_code != 200:
            return "none"
    except requests.ConnectionError:
        return "none"
    except requests.RequestException:
        return "none"
    try:
        r2 = _SESSION.get(f"{base}/v1/models", timeout=2)
        if r2.status_code == 200:
            ids = [m.get("id") for m in r2.json().get("data", [])]
            if _model_id_matches(model_name, ids):
                return "match"
    except Exception:
        pass
    return "mismatch"


def run(model_key: str = "HY"):
    """演示入口：检查配置 → 启动服务 → 发送文本请求。"""
    print("Running...")
    prompt = "按原文原格式输出"
    if not check(None, None, model_key):
        return
    if runserver(model_key):
        request(prompt, model_key)
    print("Request completed")


def check(llama, model_cfg, model_key: str = "HY") -> bool:
    """检查 vLLM 可执行文件（若配置）与模型键是否存在。"""
    print("Checking...")
    vllm_server, _, _, model_cfg2 = _vll_args()
    if not vllm_server:
        print("vllm_server 未配置（连接模式）——跳过可执行文件检查")
    elif not os.path.isfile(vllm_server):
        print(f"vllm not found at: {vllm_server}")
        return False
    model_info = model_cfg2.get(model_key) if isinstance(model_cfg2, dict) else None
    if model_info is None:
        print(f"Model key '{model_key}' not found in configuration")
        return False
    print("All checks passed")
    return True


def runserver(model_key: str = "HY", with_mmproj: bool = True, parallel: int | None = None):
    """启动 vLLM-Omni 服务并等待模型就绪（/v1/models 轮询）。

    with_mmproj 参数仅为与 llamamanage 签名一致而保留（vLLM 无 mmproj 概念，
    多模态能力由模型本身决定），调用方传什么都会被忽略。parallel 同理保留
    （vLLM 用连续批处理原生调度并发，无槽位/KV 切分概念）。
    """
    global _server_process

    vllm_server, sargs, _, _ = _vll_args()
    model_name, model_arg = _resolve_model(model_key)
    if model_name is None:
        print(f"Model key '{model_key}' not found")
        return False

    # served-model-name 覆盖探测名（vLLM /v1/models 返回 served name）
    probe_name = str(sargs.get("served_model_name") or model_name)

    probe = _probe_server(probe_name)
    if probe == "match":
        print(f"vLLM server already running with model '{probe_name}' — reusing")
        return True
    if probe == "mismatch":
        port = sargs.get("port") or "8000"
        print(f"Port {port} is occupied by a vLLM server with a different model (need '{probe_name}')")
        if _server_process is not None and _server_process.poll() is None:
            stopserver()
            time.sleep(0.5)
            if _probe_server(probe_name) != "none":
                print("Port still occupied by an external process — abort to avoid silent model mismatch; close it manually and retry")
                return False
        else:
            print("Port is held by an external process that ptoe cannot verify — abort to avoid silent model mismatch; close it manually and retry")
            return False

    if not vllm_server:
        print("vllm_server 未配置（config.json vllm_server 为空）——仅连接模式：请先在 WSL2/远程主机手动启动 vllm serve，再重试")
        return False

    args = [vllm_server, "serve", model_arg]
    # vllm_server_args 键 → --kebab-case 标志；布尔值（1/true/yes）→ 裸标志；
    # extra_args 为原始字符串，shlex 切分后原样追加
    for key, val in sargs.items():
        if key == "extra_args" or val in (None, ""):
            continue
        flag = "--" + key.replace("_", "-")
        low = str(val).strip().lower()
        if low in ("1", "true", "yes", "on"):
            args.append(flag)
        elif low in ("0", "false", "no", "off"):
            continue
        else:
            args += [flag, str(val)]
    extra = sargs.get("extra_args")
    if extra:
        args += shlex.split(str(extra))

    print(f"Starting vLLM server: {' '.join(args)}")
    try:
        _server_process = subprocess.Popen(args)
    except FileNotFoundError:
        print(f"vllm executable not found at: {vllm_server}")
        return False

    # 就绪轮询：/v1/models 返回 200 且含目标模型（/health 仅存活，加载中也 200）
    timeout = _HEALTH_TIMEOUT
    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout:
        if _server_process.poll() is not None:
            print("Server process exited unexpectedly")
            stopserver()
            return False
        try:
            resp = _SESSION.get(f"{_base_url(sargs)}/v1/models", timeout=5)
            if resp.status_code == 200:
                ids = [m.get("id") for m in resp.json().get("data", [])]
                if _model_id_matches(probe_name, ids):
                    print("Server loaded successfully")
                    return True
            time.sleep(1)
        except requests.ConnectionError:
            time.sleep(1)
        except requests.RequestException as e:
            print(f"Readiness check error: {e}")
            time.sleep(1)

    print("Server startup timed out")
    stopserver()
    return False


def stopserver():
    """停止正在运行的 vLLM 服务进程（含端口兜底：遗留/外部实例）。"""
    global _server_process

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
    _, sargs, _, _ = _vll_args()
    port = str(sargs.get("port") or "8000")
    if _kill_port_owner(port):
        print(f"Server stopped successfully (port {port})")
    else:
        print("No server process is running")
    return True


def request(prompt: str = "Hello", model_key: str = "HY", thinking: bool = False, append_ocr_instruction: bool = True):
    """向 vLLM 服务发送文本推理请求。返回 {'result': ..., 'error': ...}。"""
    try:
        if not thinking and append_ocr_instruction:
            prompt = f"{prompt}\n按原文原格式输出"
        _, sargs, _, model_cfg = _vll_args()
        model_name = (
            model_cfg.get(model_key, {}).get("name", model_key)
            if isinstance(model_cfg, dict)
            else model_key
        )
        url = f"{_base_url(sargs)}/v1/chat/completions"
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
    """读取本地图片文件并返回 base64 字符串；缺失/出错返回 None。"""
    try:
        if not os.path.exists(str(img_path)):
            return None
        with open(str(img_path), "rb") as f:
            data = f.read()
        return base64.b64encode(data).decode("utf-8")
    except Exception:
        return None


def _request_image_new(
    prompt: str,
    img,
    model_key: str = "HY",
    thinking: bool = False,
    img_is_base64: bool = False,
    timeout: int = REQUEST_TIMEOUT,
    model_name: Optional[str] = None,
    base_url: Optional[str] = None,
    img_bytes: Optional[bytes] = None,
):
    """多模态图片识别（OCR）。与 llamamanage 同签名；请求体附加
    "modalities": ["text"] 跳过音频生成阶段（vLLM-Omni 默认输出文本+音频）。

    model_name/base_url：批量识别时由 batch_infer 一次性从配置解析并传入，
    避免每页都调 _vll_args()（读 config.json + 抢全局锁）。两者都传入时
    完全跳过配置读取；否则按旧行为自行解析（单张/外部调用兜底）。
    img_bytes：可选内存图片字节，提供时跳过磁盘临时文件直接编码发送。
    """
    try:
        _mime = "image/png"
        # Use in-memory bytes if provided (skip temp file disk I/O)
        if img_bytes is not None:
            img_base64 = base64.b64encode(img_bytes).decode("utf-8")
            _mime = "image/jpeg"
        else:
            img_base64 = None
            if img_is_base64 and isinstance(img, str):
                img_base64 = img
            elif hasattr(img, "get_base64") and callable(getattr(img, "get_base64")):
                img_base64 = img.get_base64()
            else:
                img_base64 = _encode_img_local(img)

        if img_base64 is None:
            return {"result": None, "error": f"Image not found or encoding failed: {img}"}

        if img_bytes is None:
            _img_path = (
                img if (isinstance(img, str) and not img_is_base64) else getattr(img, "path", "")
            )
            _mime = "image/jpeg" if str(_img_path).lower().endswith((".jpg", ".jpeg")) else "image/png"

        prompt_ = f"{prompt}\n按原文原格式输出" if not thinking else prompt

        if model_name is None or base_url is None:
            _, sargs, _, model_cfg = _vll_args()
            if base_url is None:
                base_url = _base_url(sargs)
            if model_name is None:
                model_name = (
                    model_cfg.get(model_key, {}).get("name", model_key)
                    if isinstance(model_cfg, dict)
                    else model_key
                )
        url = f"{base_url}/v1/chat/completions"
        data = {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_},
                        {"type": "image_url", "image_url": {"url": f"data:{_mime};base64,{img_base64}"}},
                    ],
                }
            ],
            "stream": False,
            "max_tokens": MAX_TOKENS,
            "chat_template_kwargs": {"enable_thinking": thinking},
            "modalities": ["text"],
        }
        headers = {"Content-Type": "application/json"}
        resp = _SESSION.post(url, json=data, headers=headers, timeout=timeout)
        resp.raise_for_status()
        result = resp.json()
        if "choices" in result and result["choices"]:
            choice = result["choices"][0]
            if choice.get("finish_reason") == "length":
                print(f"[request_image] WARNING: {img} hit max_tokens={MAX_TOKENS} (finish_reason=length) — 输出可能被截断，请检查该页内容")
            return {"result": choice["message"]["content"], "error": None}
        return {"result": None, "error": f"No choices in response: {result}"}
    except Exception as e:
        print(f"[request_image] Request failed for {img}: {e}")
        return {"result": None, "error": str(e)}


# 兼容 llamamanage 的旧名绑定（test_image_queue_request.py 按位置参数调用）
request_image = _request_image_new


def batch_infer(images, prompts, model_key: str = "HY", max_workers: Optional[int] = None,
                thinking: bool = False, timeout: int = REQUEST_TIMEOUT, on_progress=None,
                on_result=None):
    """批量图片 OCR（与 llamamanage.batch_infer 同构，调用本模块的 _request_image_new）。

    max_workers=None 时取模型推荐并发（model_choices.<key>.workers，缺省 3）；
    base_url/model_name 批次开始解析一次，批内每页请求不再读配置。
    """
    def infer_one(img, prompt):
        t0 = time.perf_counter()
        try:
            r = _request_image_new(
                prompt, img, model_key, thinking=thinking, timeout=timeout,
                model_name=model_name, base_url=base_url,
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

    model_name: Optional[str] = None
    base_url: Optional[str] = None
    try:
        _, sargs, _, model_cfg = _vll_args()
        base_url = _base_url(sargs)
        model_name = (
            model_cfg.get(model_key, {}).get("name", model_key)
            if isinstance(model_cfg, dict)
            else model_key
        )
        max_workers = _resolve_workers(model_cfg, model_key, max_workers)
    except Exception as e:
        msg = f"config resolution failed: {e}"
        print(f"[batch_infer] {msg}")
        return [{"img": img, "result": None, "error": msg} for img in images]

    if isinstance(prompts, str):
        prompts = [prompts] * len(images)
    if len(prompts) == 1:
        prompts = prompts * len(images)
    assert len(images) == len(prompts), "images/prompts 必须等长"

    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fut_map = {pool.submit(infer_one, img, prompts[i]): i for i, img in enumerate(images)}
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