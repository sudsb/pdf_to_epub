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
import threading

_CFG_LOCK = threading.Lock()  # 避免并发写
_CONFIG_PATH = "config.json"
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


def get_config():
    """统一入口，返回健壮配置(dict)，丢失/坏则自动生成/修复。线程安全。

    If llama_server or models_dir are missing or point to non-existent paths,
    prompt the user with a file/directory chooser to locate them. This keeps
    the configuration interactive instead of relying on hardcoded paths.
    """
    with _CFG_LOCK:
        try:
            if os.path.exists(_CONFIG_PATH):
                with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            else:
                cfg = DEFAULT_CONFIG.copy()
                with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
            newcfg = validate_and_patch_config(cfg)
            # 回填新字段
            if newcfg != cfg:
                with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(newcfg, f, ensure_ascii=False, indent=2)

            # Interactive prompts for missing/invalid paths
            # Only prompt when running in a GUI-capable environment; tkinter will raise otherwise.
            try:
                llama_path = newcfg.get("llama_server")
                if not llama_path or not os.path.isfile(llama_path):
                    chosen = getfilepath()
                    if chosen:
                        newcfg["llama_server"] = chosen
                models_path = newcfg.get("models_dir")
                if not models_path or not os.path.isdir(models_path):
                    chosen = getdicpath()
                    if chosen:
                        newcfg["models_dir"] = chosen
                # persist any interactive choices
                with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(newcfg, f, ensure_ascii=False, indent=2)
            except Exception:
                # headless or tkinter unavailable — skip interactive prompts
                pass

            return newcfg
        except Exception as e:
            # 配置文件被损坏，回退
            print(f"[config] Error reading config, fallback to default: {e}")
            cfg = DEFAULT_CONFIG.copy()
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            return cfg
        except Exception as e:
            # 配置文件被损坏，回退
            print(f"[config] Error reading config, fallback to default: {e}")
            cfg = DEFAULT_CONFIG.copy()
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            return cfg


def update_config(key, value):
    """更新单个配置字段，并持久化（线程安全）."""
    with _CFG_LOCK:
        cfg = get_config()
        cfg[key] = value
        cfg = validate_and_patch_config(cfg)
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return cfg
