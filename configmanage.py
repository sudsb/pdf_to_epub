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
    # 各模型推荐 OCR 并发（workers，可选）：batch_infer 未显式指定并发时按此
    # 值运行（--workers 或 GUI 转换页显式指定则覆盖）。依据模型大小/量化选择：
    # 大模型（HY BF16）显存压力大，并发过高会让多槽位 KV 缓存溢出到 CPU、
    # 单张耗时反而大涨，宜 2-3；小模型（0.8B）显存占用小，可 6+。设置页
    # 「模型管理」表格可逐模型调整。
    "model_choices": {
        "HY": {
            "name": "HunyuanOCR.BF16.gguf",
            "mmproj": "HunyuanOCR.mmproj-bf16.gguf",
            "workers": 2,
        },
        "QWEN.8": {
            "name": "Qwen3.5-0.8B-Q8_0.gguf",
            "mmproj": "0.8b_mmproj-F16.gguf",
            "workers": 6,
        },
        "QWEN2": {
            "name": "Qwen3.5-2B-Q8_0.gguf",
            "mmproj": "2b_mmproj-F16.gguf",
            "workers": 4,
        },
        "QWEN4": {
            "name": "Qwen3.5-4B-Q8_0.gguf",
            "mmproj": "4b_mmproj-F16.gguf",
            "workers": 3,
        },
        "PD": {
            "name": "PaddleOCR-VL-1.6-GGUF.gguf",
            "mmproj": "PaddleOCR-VL-1.6-GGUF-mmproj.gguf",
            "workers": 4,
        },
        "ULQ8": {
            "name": "Unlimited-OCR-Q8_0.gguf",
            "mmproj": "mmproj-Unlimited-OCR-F16.gguf",
            "workers": 3,
        },
        "ULQ4": {
            "name": "Unlimited-OCR-Q4_K_S.gguf",
            "mmproj": "mmproj-Unlimited-OCR-F16.gguf",
            "workers": 5,
        },
    },
        "proofread": {
            "similarity_min": 0.6,
            "score_min": 0.4,
            "max_cand_cache": 2000,
            # LLM-assisted proofreading (disabled by default). UI toggle stored in browser localStorage;
            # when enabled the client sends use_llm=true and optional llm_model override.
            "enable_llm": False,
            # 原有规则开关（2026-08-09）：False（默认）时「校正」只跑三条新规则
            # （连续重复 / 连续标点 / 中文中的连续字母）；True 时额外跑半角转全角、
            # 引号配对、混淆表、词典滑窗四条原有规则。经 /api/proofread_settings 读写。
            "enable_legacy_rules": False,
            "llm_model": "qwen2b",
            "llm_timeout": 3
        },
    # 若用户配置里显式给出 n_gpu_layers 键，则覆盖自动探测。
    "llama_server_args": {
        "host": "127.0.0.1",
        "port": "8080",
        "temperature": "0",
        "repeat_penalty": "1.1",
        # parallel（槽位数）：KV cache 总量 ≈ ctx × parallel，槽位多于实际并发
        # 只会浪费显存（溢出到 CPU 时单页反而变慢）。默认 4 匹配常见并发；
        # 流程运行时 runserver 还会按实际 workers 取 min 自适应（见 llamamanage）。
        "parallel": "4",
        "cache_type_k": "q8_0",
        "cache_type_v": "q8_0",
        "log_verbosity": "0",
        # max_tokens 保留：请求级 MAX_TOKENS 上限（OCR 单页输出远小于此值）。
        # ngram_size/window_size 已移除：纯启动参数，部分 llama-server 构建不支持
        # （如 llama13 会因 --ngram-size/--window-size 直接退出），且对 OCR 无增益。
        # flash_attn 可选："0"/false 禁用、空/缺省自动、非 0（如 "1"/"on"）强制开启
        # GPU 下的 Flash Attention（新构建默认 auto：CUDA 支持时自动开启；
        # 老构建自动附加裸 --flash-attn）。
        "max_tokens": "8192",   # per-request max token cap (also passed to llama-server via --max-tokens)
    },
    # 推理引擎选择：'llama'（llama.cpp，默认）| 'vllm'（vLLM-Omni）
    "engine": "llama",
    # vLLM-Omni 可执行文件路径（如 "vllm" 或绝对路径）；空 = 仅连接模式
    # （vLLM-Omni 官方仅支持 Linux，Windows 用户可在 WSL2/远程手动启动后连接）
    "vllm_server": "",
    # vllm serve 启动参数（键 → --kebab-case 标志；"1"/"true"/"yes" → 裸标志；
    # extra_args 为原始字符串，shlex 切分后原样追加）
    "vllm_server_args": {
        "host": "127.0.0.1",
        "port": "8000",
        "omni": "1",
        "trust_remote_code": "1",
        "served_model_name": "",
        "max_model_len": "",
        "gpu_memory_utilization": "",
        "limit_mm_per_prompt": "",
        "chat_template": "",
        "mm_proj_config": "",
        "mm_processor_config": "",
        "deploy_config": "",
        "extra_args": "",
    },
    # 矫正界面快捷键绑定（op -> 组合键字符串）。随机端口下 localStorage 每次运行失效，
    # 故持久化到 config.json（经 /api/shortcuts GET/POST 读写）。
    "shortcuts": {},
    # 矫正界面格式规则（弹窗管理）：新模型每条 {id, name, mode(first|all), conditions:[{type, pattern, scope, formats}]}；
    # 旧模型 {id, name, formats, condition, else_formats} 读取时由 correctmanage._validate_format_rules 迁移
    "format_rules": [],
    # 字体设置（2026-08）：正文/标题/注释/引用 独立字体，供 CSS 变量使用
     "fonts": {
        "body": "serif",
        "heading": "sans-serif",
        "note": "serif",
        "citation": "cursive"
    },
    # 引用字体是否默认斜体（2026-08）
    "citationItalicEnabled": True,
    # 图片预处理（2026-08，OpenCV）：PDF 分割图片时启用，提高 OCR 识别率。
    # enabled 开关；gray 灰度 / denoise 中值去噪 / sharpen 锐化 / binarize 自适应二值化；
    # workers 0=自动按 CPU 核数（>0 时限制渲染进程数）。设置变更会使 .ptoe_split.json 缓存失效。
    "image_preprocess": {
        "enabled": False,
        "gray": True,
        "denoise": True,
        "sharpen": True,
        "binarize": False,
        "workers": 0
    },
    # OCR 排除页码：列表形式，如 [1, 2, 5, 10-15]（解析后展开为单个页码）。
    # 从 config.json 读取时由 parse_exclude_spec 处理字符串/列表兼容；
    # CLI --exclude 优先于配置文件。
    "exclude_pages": [],
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
            # Respect user's explicit model_choices dict: do NOT merge default
            # model choices back in, otherwise removed built-ins reappear.
            elif k == "model_choices":
                continue
            elif isinstance(v, dict):
                merge(d[k], v)


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


def find_canonical_model_key(choices: dict, key: str):
    """Return (canonical_key, matches).

    - If `key` exactly equals an existing key in `choices`, return (key, [key]).
    - Else, perform a case-insensitive match among string keys and return (canonical_key, [matches])
      when there's exactly one case-insensitive match.
    - If multiple case-insensitive matches exist, return (None, matches).
    - If no matches, return (None, []).

    This helper centralizes case-insensitive model key resolution so callers (GUI/API/CLI)
    can uniformly map user-provided values to stored canonical keys.
    """
    if not isinstance(choices, dict) or not isinstance(key, str) or not key.strip():
        return None, []
    # exact match wins
    if key in choices:
        return key, [key]
    # case-insensitive matches
    matches = [k for k in choices.keys() if isinstance(k, str) and k.lower() == key.lower()]
    if len(matches) == 1:
        return matches[0], matches
    return None, matches

def update_config(key, value):
    """Update a single top-level config key and persist atomically with minimized lock hold.

    Strategy (optimistic update):
    1. Read config from disk without holding _CFG_LOCK (fallback to DEFAULT_CONFIG).
    2. Apply the requested change and run validate_and_patch_config() outside the lock.
       Validation is allowed to be more expensive but must not hold the global _CFG_LOCK.
    3. Acquire _CFG_LOCK briefly, re-read the on-disk config, re-apply the requested change to the
       fresh config (to avoid races), validate again, then persist via _atomic_write_json().

    Special-case: model_choices is treated as caller-authoritative and written without merging
    DEFAULT_CONFIG entries back in (preserves existing behavior).
    """
    # 1) optimistic read (no lock)
    try:
        if os.path.exists(_CONFIG_PATH):
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg_disk = json.load(f)
        else:
            cfg_disk = DEFAULT_CONFIG.copy()
    except Exception as e:
        print(f"[config] Error reading config for update_config (optimistic): {e}")
        cfg_disk = DEFAULT_CONFIG.copy()

    # 2) special-case model_choices: caller-provided dict is authoritative
    if key == "model_choices" and isinstance(value, dict):
        with _CFG_LOCK:
            try:
                if os.path.exists(_CONFIG_PATH):
                    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                        latest = json.load(f)
                else:
                    latest = DEFAULT_CONFIG.copy()
                latest[key] = value
                _atomic_write_json(_CONFIG_PATH, latest)
                return latest
            except Exception as e:
                print(f"[config] Error updating model_choices: {e}")
                latest = DEFAULT_CONFIG.copy()
                with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(latest, f, ensure_ascii=False, indent=2)
                return latest

    # 3) apply change + validate outside lock
    try:
        cfg_candidate = cfg_disk.copy()
        cfg_candidate[key] = value
        cfg_candidate = validate_and_patch_config(cfg_candidate)
    except Exception as e:
        print(f"[config] Error validating candidate config: {e}")
        cfg_candidate = DEFAULT_CONFIG.copy()
        cfg_candidate[key] = value

    # 4) brief critical section: re-read, re-apply change, validate, write
    with _CFG_LOCK:
        try:
            if os.path.exists(_CONFIG_PATH):
                with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                    latest = json.load(f)
            else:
                latest = DEFAULT_CONFIG.copy()
            latest[key] = value
            latest = validate_and_patch_config(latest)
            _atomic_write_json(_CONFIG_PATH, latest)
            return latest
        except Exception as e:
            print(f"[config] Error updating config under lock, fallback to default: {e}")
            latest = DEFAULT_CONFIG.copy()
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(latest, f, ensure_ascii=False, indent=2)
            return latest



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


def set_vllm_server_arg(name: str, value) -> dict:
    """设置 vLLM-Omni 启动参数（嵌套键 vllm_server_args.<name>）并持久化。

    与 set_llama_server_arg 同构：锁内读配置、确保 vllm_server_args 存在、
    改值、校验、原子写回，返回新配置。线程安全。
    """
    with _CFG_LOCK:
        try:
            if os.path.exists(_CONFIG_PATH):
                with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            else:
                cfg = DEFAULT_CONFIG.copy()
            if not isinstance(cfg.get("vllm_server_args"), dict):
                cfg["vllm_server_args"] = dict(
                    DEFAULT_CONFIG.get("vllm_server_args", {})
                )
            cfg["vllm_server_args"][name] = value
            cfg = validate_and_patch_config(cfg)
            _atomic_write_json(_CONFIG_PATH, cfg)
            return cfg
        except Exception as e:
            print(f"[config] Error updating vllm_server_args, fallback to default: {e}")
            cfg = DEFAULT_CONFIG.copy()
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            return cfg


def set_proofread_param(name: str, value) -> dict:
    """设置 proofread 嵌套配置（proofread.<name>），并持久化。

    支持数值自动转换（整型/浮点），其余以字符串保存。
    """
    # try to coerce numeric values to int/float
    v: object = value
    try:
        if isinstance(value, str) and value.isdigit():
            v = int(value)
        else:
            if isinstance(value, str) and ("." in value or "e" in value.lower()):
                v = float(value)
    except Exception:
        v = value

    with _CFG_LOCK:
        try:
            if os.path.exists(_CONFIG_PATH):
                with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            else:
                cfg = DEFAULT_CONFIG.copy()
            if not isinstance(cfg.get("proofread"), dict):
                cfg["proofread"] = dict(DEFAULT_CONFIG.get("proofread", {}))
            cfg["proofread"][name] = v
            cfg = validate_and_patch_config(cfg)
            _atomic_write_json(_CONFIG_PATH, cfg)
            return cfg
        except Exception as e:
            print(f"[config] Error updating proofread param, fallback to default: {e}")
            cfg = DEFAULT_CONFIG.copy()
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            return cfg


def set_format_rules(rules: list) -> dict:
    """设置矫正界面格式规则（顶层键 format_rules）并持久化。

    与 set_shortcuts 同构：锁内读配置、改值、校验、原子写回，返回新配置。
    rules 必须是 list。新模型每项含 name / mode / conditions（conditions 为有序
    条件列表，每项 {type, pattern, scope, formats}）；旧模型（formats /
    condition / else_formats）原样保留，读取时由 correctmanage 迁移。
    线程安全。
    """
    if not isinstance(rules, list):
        raise ValueError("format_rules 必须是数组")
    clean = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        item = {
            "id": str(r.get("id") or ""),
            "name": str(r.get("name") or ""),
        }
        if "conditions" in r:
            # 新模型：保留 mode + conditions（已由 correctmanage._validate_format_rules 清洗）
            item["mode"] = str(r.get("mode") or "first")
            item["conditions"] = []
            for c in r.get("conditions"):
                if not isinstance(c, dict):
                    continue
                cond_out: dict = {
                    "type": str(c.get("type") or "contains"),
                    "pattern": str(c.get("pattern") or ""),
                    "scope": str(c.get("scope") or "selection"),
                    "formats": [str(x) for x in (c.get("formats") or [])],
                }
                # target: 匹配对象/条件之前/条件之后/两条件之间
                target = str(c.get("target") or "match")
                if target not in ("match", "before", "after", "between"):
                    target = "match"
                cond_out["target"] = target
                if target == "between":
                    cond_out["between_end_pattern"] = str(c.get("between_end_pattern") or "")
                # 正则条件可携带 group_formats：每个捕获组独立格式列表
                gf = c.get("group_formats")
                if isinstance(gf, list) and gf:
                    cond_out["group_formats"] = [
                        [str(x) for x in (sub if isinstance(sub, list) else [])]
                        for sub in gf
                    ]
                item["conditions"].append(cond_out)
        else:
            # 旧模型：原样保留（读取时迁移）
            item["formats"] = [str(x) for x in (r.get("formats") or [])]
            item["condition"] = (
                r.get("condition")
                if isinstance(r.get("condition"), dict)
                else {"enabled": False}
            )
            item["else_formats"] = [str(x) for x in (r.get("else_formats") or [])]
        clean.append(item)
    with _CFG_LOCK:
        try:
            if os.path.exists(_CONFIG_PATH):
                with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            else:
                cfg = DEFAULT_CONFIG.copy()
            cfg["format_rules"] = clean
            cfg = validate_and_patch_config(cfg)
            _atomic_write_json(_CONFIG_PATH, cfg)
            return cfg
        except Exception as e:
            print(f"[config] Error updating format_rules, fallback to default: {e}")
            cfg = DEFAULT_CONFIG.copy()
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            return cfg


def set_shortcuts(shortcuts: dict) -> dict:
    """设置矫正界面快捷键绑定（顶层键 shortcuts）并持久化。

    与 set_proofread_param 同构：锁内读配置、改值、校验、原子写回，返回新配置。
    仅在确有变更时写盘（快捷键设置页每次录制都会 POST，避免无谓磁盘写）。
    线程安全。
    """
    if not isinstance(shortcuts, dict):
        raise ValueError("shortcuts 必须是对象（op -> 组合键）")
    clean = {str(k): ("" if v is None else str(v)) for k, v in shortcuts.items()}
    with _CFG_LOCK:
        try:
            if os.path.exists(_CONFIG_PATH):
                with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            else:
                cfg = DEFAULT_CONFIG.copy()
            before = cfg.get("shortcuts")
            cfg["shortcuts"] = clean
            cfg = validate_and_patch_config(cfg)
            if before != clean:  # 无变更不写盘
                _atomic_write_json(_CONFIG_PATH, cfg)
            return cfg
        except Exception as e:
            print(f"[config] Error updating shortcuts, fallback to default: {e}")
            cfg = DEFAULT_CONFIG.copy()
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            return cfg
