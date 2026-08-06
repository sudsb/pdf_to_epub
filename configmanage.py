import json
import os
import tkinter as tk
from tkinter import filedialog


def getconfig():
    # backward-compatible alias -> returns validated config
    return get_config()


def creatconfig(lpath, mpath, mlist, cmodel):
    # Legacy compatibility: map to new config
    cfg = get_config()
    if lpath:
        cfg["llama_server"] = lpath
    if mpath:
        cfg["models_dir"] = mpath
    if mlist:
        try:
            import json as _json

            mlist_obj = _json.loads(mlist) if isinstance(mlist, str) else mlist
            if isinstance(mlist_obj, dict):
                cfg["model_choices"] = mlist_obj
        except Exception:
            pass
    if cmodel:
        cfg["selected_model"] = cmodel
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return True


def saveconfig(lpath=None, mpath=None, mlist=None, cmodel=None):
    cfg = get_config()
    if lpath:
        cfg["llama_server"] = lpath
    if mpath:
        cfg["models_dir"] = mpath
    if mlist:
        try:
            import json as _json

            mlist_obj = _json.loads(mlist) if isinstance(mlist, str) else mlist
            if isinstance(mlist_obj, dict):
                cfg["model_choices"] = mlist_obj
        except Exception:
            pass
    if cmodel:
        cfg["selected_model"] = cmodel
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return True


def getfilepath():
    root = tk.Tk()
    root.withdraw()
    file_path = filedialog.askopenfilename(title="选择文件")
    return file_path


def getdicpath():
    root = tk.Tk()
    root.withdraw()
    dic_path = filedialog.askdirectory(title="选择文件夹")
    return dic_path


# ---- 集中式统一配置 ----
import tempfile
import threading

_CFG_LOCK = threading.Lock()  # 避免并发写
_CONFIG_PATH = "config.json"


def _atomic_write_json(path: str, obj: dict) -> None:
    """原子写 JSON：先写临时文件再 os.replace，避免中途崩溃/被杀留下半截文件。

    兼作性能优化：写入只在确有变更时发生（调用方负责判断），配合
    临时文件 + 原子替换，任何时刻磁盘上的 config.json 都是完整可读的。
    """
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise
DEFAULT_CONFIG = {
    "llama_server": "E:/xox/Tools/llama-c/llama-server.exe",
    "models_dir": "E:/xox/Tools/llama-c/models",
    "model_choices": {
        "HY": {"name": "HunyuanOCR.BF16.gguf", "mmproj": "HunyuanOCR.mmproj-bf16.gguf"},
        "QWEN.8": {"name": "Qwen3.5-0.8B-Q8_0.gguf", "mmproj": "0.8b_mmproj-F16.gguf"},
        "QWEN2": {"name": "Qwen3.5-2B-Q8_0.gguf", "mmproj": "2b_mmproj-F16.gguf"},
        "QWEN4": {"name": "Qwen3.5-4B-Q8_0.gguf", "mmproj": "4b_mmproj-F16.gguf"},
        "PD": {
            "name": "PaddleOCR-VL-1.6-GGUF.gguf",
            "mmproj": "PaddleOCR-VL-1.6-GGUF-mmproj.gguf",
        },
        "ULQ8": {
            "name": "Unlimited-OCR-Q8_0.gguf",
            "mmproj": "mmproj-Unlimited-OCR-F16.gguf",
        },
        "ULQ4": {
            "name": "Unlimited-OCR-Q4_K_S.gguf",
            "mmproj": "mmproj-Unlimited-OCR-F16.gguf",
        },
    },
    "selected_model": "HY",
    # OCR 提示词：与 llamamanage.OCR_PROMPT 保持一致，作为 config.json 缺失时的
    # 兜底种子（首次加载会写入 config.json，之后以配置值为准）。
    "ocr_prompt": "请逐行完整识别图片中的全部文字，逐字输出，不得遗漏任何内容、不得省略、不得总结、不得翻译",
    # llama-server 启动参数：镜像 runserver() 当前硬编码参数（值以字符串存储，
    # 原样传给 subprocess）。n_gpu_layers 不在此默认值中（保持自动检测）；
    # 若用户配置里显式给出 n_gpu_layers 键，则覆盖自动探测。
    "llama_server_args": {
        "host": "127.0.0.1",
        "port": "8080",
        "temperature": "0",
        "repeat_penalty": "1.1",
        "parallel": "11",
        "cache_type_k": "q8_0",
        "cache_type_v": "q8_0",
        "log_verbosity": "0",
    },
    # 可拓展其余各manage/key conf
}


def validate_and_patch_config(cfg):
    """确保返回的配置dict所有字段完整，无则回补DEFAULT_CONFIG."""
    if cfg is None:
        return DEFAULT_CONFIG.copy()

    def merge(d, default):
        for k, v in default.items():
            if k not in d:
                d[k] = v
            elif isinstance(v, dict):
                merge(d[k], v)
        # 删掉冗余老字段（可选）

    out = dict(cfg)  # 浅拷贝
    merge(out, DEFAULT_CONFIG)
    # 校验selected_model
    choices = out.get("model_choices", {})
    sel = out.get("selected_model")
    if sel not in choices:
        out["selected_model"] = next(iter(choices)) if choices else None
    # llama_server/models_dir等类型检查＋回退
    if not isinstance(out.get("llama_server"), str):
        out["llama_server"] = DEFAULT_CONFIG["llama_server"]
    if not isinstance(out.get("models_dir"), str):
        out["models_dir"] = DEFAULT_CONFIG["models_dir"]
    return out


def get_config(*, show_dialogs: bool = True):
    """统一入口，返回健壮配置(dict)，丢失/坏则自动生成/修复。线程安全。

    show_dialogs=True 时，若 llama_server/models_dir 缺失或指向不存在的路径，
    弹出文件/目录选择框让用户定位（默认行为）。CLI/headless 读取传
    show_dialogs=False 可跳过交互弹窗（校验与回填默认值仍照常执行）。

    If llama_server or models_dir are missing or point to non-existent paths,
    prompt the user with a file/directory chooser to locate them. This keeps
    the configuration interactive instead of relying on hardcoded paths.
    """
    # 锁内只做「读/校验/确有变更才写盘」；tkinter 对话框移出锁外（S2），
    # 否则对话框期间其他线程的 get_config 会卡死在锁上。
    with _CFG_LOCK:
        try:
            if os.path.exists(_CONFIG_PATH):
                with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            else:
                cfg = DEFAULT_CONFIG.copy()
                _atomic_write_json(_CONFIG_PATH, cfg)
            newcfg = validate_and_patch_config(cfg)
            # 只在确有字段变更时写盘（避免每次调用都无条件全量重写）
            if newcfg != cfg:
                _atomic_write_json(_CONFIG_PATH, newcfg)
        except Exception as e:
            # 配置文件被损坏，回退默认并重建
            print(f"[config] Error reading config, fallback to default: {e}")
            newcfg = DEFAULT_CONFIG.copy()
            _atomic_write_json(_CONFIG_PATH, newcfg)

    # Interactive prompts for missing/invalid paths（锁外：弹窗期间不阻塞其他线程）
    # Only prompt when running in a GUI-capable environment; tkinter will raise otherwise.
    # show_dialogs=False 时跳过弹窗（CLI/headless 读取），校验与回填仍照常执行。
    changed = False
    if show_dialogs:
        try:
            llama_path = newcfg.get("llama_server")
            if not llama_path or not os.path.isfile(llama_path):
                chosen = getfilepath()
                if chosen:
                    newcfg["llama_server"] = chosen
                    changed = True
            models_path = newcfg.get("models_dir")
            if not models_path or not os.path.isdir(models_path):
                chosen = getdicpath()
                if chosen:
                    newcfg["models_dir"] = chosen
                    changed = True
        except Exception:
            # headless or tkinter unavailable — skip interactive prompts
            pass
    if changed:
        # persist any interactive choices（锁内写回，保证并发安全）
        with _CFG_LOCK:
            _atomic_write_json(_CONFIG_PATH, newcfg)
    return newcfg


def update_config(key, value):
    """更新单个配置字段，并持久化（线程安全）。

    注意：不要在持有 _CFG_LOCK 时调用 get_config()，因为 get_config
    本身会尝试获取同一锁（导致死锁）。直接读取/写入配置文件并
    使用 validate_and_patch_config 做校验。
    """
    with _CFG_LOCK:
        try:
            if os.path.exists(_CONFIG_PATH):
                with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            else:
                cfg = DEFAULT_CONFIG.copy()
            cfg[key] = value
            cfg = validate_and_patch_config(cfg)
            _atomic_write_json(_CONFIG_PATH, cfg)
            return cfg
        except Exception as e:
            print(f"[config] Error updating config, fallback to default: {e}")
            cfg = DEFAULT_CONFIG.copy()
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            return cfg


def set_ocr_prompt(prompt: str) -> dict:
    """设置 OCR 提示词（顶层键 ocr_prompt）并持久化，返回新配置。线程安全。

    空串/None 时回退到 DEFAULT_CONFIG 的默认提示词（validate_and_patch_config
    会补齐缺失键，但显式写空串也会被校验逻辑保留，故此处直接透传）。
    """
    return update_config("ocr_prompt", prompt)


def set_llama_server_arg(name: str, value) -> dict:
    """设置 llama-server 启动参数（嵌套键 llama_server_args.<name>）并持久化。

    注意：update_config 只处理顶层键，嵌套参数需在此直接实现——锁内读配置、
    确保 llama_server_args 存在、改值、校验、原子写回，返回新配置。线程安全。
    """
    with _CFG_LOCK:
        try:
            if os.path.exists(_CONFIG_PATH):
                with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            else:
                cfg = DEFAULT_CONFIG.copy()
            if not isinstance(cfg.get("llama_server_args"), dict):
                cfg["llama_server_args"] = dict(
                    DEFAULT_CONFIG.get("llama_server_args", {})
                )
            cfg["llama_server_args"][name] = value
            cfg = validate_and_patch_config(cfg)
            _atomic_write_json(_CONFIG_PATH, cfg)
            return cfg
        except Exception as e:
            print(f"[config] Error updating llama_server_args, fallback to default: {e}")
            cfg = DEFAULT_CONFIG.copy()
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            return cfg
