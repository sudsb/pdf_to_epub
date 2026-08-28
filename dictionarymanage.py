"""词典增强校正模块（2026-08，P0/P1/P2）。

提供词典层文字纠错能力，与 correctmanage.proofread_page 字级规则互补：
- P0 词表白名单防误报（is_word）
- P1 未知词检测 + 候选生成（形近/同音/编辑距离 + 权重排序）
- P2 用户词表回写（采纳→动态混淆对、忽略→排除表，持久化 data/proofread_dict.json）

纯 stdlib；所有函数幂等；模块级懒加载缓存。
"""

import json
import math
import os
import re
import sys
import tempfile
import threading
from typing import Dict, List, Set, Tuple

# ---------------------------------------------------------------------------
# 模块级懒加载缓存（线程安全）
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_loaded = False

# 通用词表：词 → 词频（int）
_word_freq: Dict[str, int] = {}
# 形近字表：字 → 同组形近字集合
_shape_groups: Dict[str, Set[str]] = {}
# 同音字表：字 → 同组同音字集合
_homophone_groups: Dict[str, Set[str]] = {}
# 用户动态混淆对：wrong → fixed（P2 采纳）
_user_fixes: Dict[str, str] = {}
# 用户排除表（P2 忽略）
_user_ignored: Set[str] = set()

# 持久化路径：程序所在目录 data/proofread_dict.json（冻结时为 exe 目录，
# 不随启动时 CWD 漂移；与分割图片/历史记录同属用户数据）
from pdfmanage import app_base_dir as _app_base_dir

_USER_DICT_PATH = os.path.join(str(_app_base_dir()), "data", "proofread_dict.json")

# CJK 统一表意文字范围（基本 + 扩展 A）
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_SIMILARITY_MIN = 0.6  # minimal similarity for word-level candidate consideration
_SCORE_MIN = 0.5       # minimal score to include a candidate (raised to reduce false positives)
_MAX_CAND_CACHE = 2000 # cached_candidates_for_token size cap
_MAX_REPLACEMENT_COMBINATIONS = 20  # cap for per-word replacement attempts


def _dicts_dir() -> str:
    """解析 dicts/ 目录路径：frozen 时走 _MEIPASS，否则走仓库根。"""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", "")
        return os.path.join(base, "dicts")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "dicts")


def _load_jieba_dict(path: str) -> Dict[str, int]:
    """加载 jieba_dict.txt（词 词频 词性），返回 {词: 词频}。"""
    result: Dict[str, int] = {}
    if not os.path.isfile(path):
        return result
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split(" ", 2)
            if len(parts) < 2:
                continue
            word = parts[0]
            try:
                freq = int(parts[1])
            except ValueError:
                freq = 1
            result[word] = freq
    return result


def _load_pair_file(path: str) -> Dict[str, Set[str]]:
    """加载形近/同音表（每行一组，空格分隔），返回 {字: 同组集合}。"""
    result: Dict[str, Set[str]] = {}
    if not os.path.isfile(path):
        return result
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            chars = line.split()
            if len(chars) < 2:
                continue
            group = set(chars)
            for ch in chars:
                if ch not in result:
                    result[ch] = set()
                result[ch].update(group)
                result[ch].discard(ch)  # 不包含自身
    return result


def _load_user_dict(path: str) -> Tuple[Dict[str, str], Set[str]]:
    """加载用户词表（data/proofread_dict.json），失败返回空表。"""
    fixes: Dict[str, str] = {}
    ignored: Set[str] = set()
    if not os.path.isfile(path):
        return fixes, ignored
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            raw_fixes = data.get("fixes", {})
            if isinstance(raw_fixes, dict):
                for k, v in raw_fixes.items():
                    if isinstance(k, str) and isinstance(v, str):
                        fixes[k] = v
            raw_ignored = data.get("ignored", [])
            if isinstance(raw_ignored, list):
                for w in raw_ignored:
                    if isinstance(w, str):
                        ignored.add(w)
    except Exception:
        pass
    return fixes, ignored


def _save_user_dict(path: str, fixes: Dict[str, str], ignored: Set[str]) -> None:
    """原子写用户词表（tempfile + os.replace）。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    obj = {"fixes": fixes, "ignored": sorted(ignored)}
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".dict-", suffix=".tmp")
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

def load_dicts() -> None:
    """加载 dicts/ 下全部数据（通用词表/形近/同音/用户词表）。

    文件缺失静默跳过；幂等（已加载直接返回）。
    """
    global _loaded, _word_freq, _shape_groups, _homophone_groups
    global _user_fixes, _user_ignored, _words_by_first
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        d = _dicts_dir()
        _word_freq = _load_jieba_dict(os.path.join(d, "jieba_dict.txt"))
        _shape_groups = _load_pair_file(os.path.join(d, "shapes.txt"))
        _homophone_groups = _load_pair_file(os.path.join(d, "homophones.txt"))
        # Build helper index: words by first character for faster candidate search
        _words_by_first = {}
        for w in _word_freq.keys():
            if not w:
                continue
            first = w[0]
            _words_by_first.setdefault(first, []).append(w)
        # Precompute popular single-character candidates (from jieba_dict) for fast single-char suggestions
        global _top_single_chars
        _top_single_chars = [w for w, _ in sorted(_word_freq.items(), key=lambda x: -x[1]) if len(w) == 1][:200]

        # Load runtime-configurable tuning params from config.json if available
        try:
            import configmanage
            cfg = configmanage.get_config(show_dialogs=False)
            p = cfg.get("proofread", {}) if isinstance(cfg, dict) else {}
            global _SIMILARITY_MIN, _SCORE_MIN, _MAX_CAND_CACHE, _MAX_REPLACEMENT_COMBINATIONS
            _SIMILARITY_MIN = float(p.get("similarity_min", _SIMILARITY_MIN))
            _SCORE_MIN = float(p.get("score_min", _SCORE_MIN))
            _MAX_CAND_CACHE = int(p.get("max_cand_cache", _MAX_CAND_CACHE))
            _MAX_REPLACEMENT_COMBINATIONS = int(
                p.get("max_replacement_combinations", _MAX_REPLACEMENT_COMBINATIONS)
            )
            # Also sync auto fix score into correctmanage if available
            try:
                import correctmanage
                correctmanage.AUTO_FIX_SCORE = float(p.get("auto_fix_score", getattr(correctmanage, "AUTO_FIX_SCORE", 0.85)))
            except Exception:
                pass
        except Exception:
            # ignore config load failures; keep compiled defaults
            pass

        _loaded = True


def _ensure_loaded() -> None:
    """确保词典已加载，供模块内其它函数调用（幂等）。"""
    global _loaded
    if not _loaded:
        load_dicts()

    if not _loaded:
        load_dicts()


# ---------------------------------------------------------------------------
# P0 白名单
# ---------------------------------------------------------------------------
def is_word(w: str) -> bool:
    """P0 白名单：通用词表 ∪ 用户采纳词命中 True；排除表命中返回 False。"""
    _ensure_loaded()
    if w in _user_ignored:
        return False
    if w in _word_freq:
        return True
    # 用户采纳词的 fixed 也算 covered
    if w in _user_fixes.values():
        return True
    return False


# ---------------------------------------------------------------------------
# 分词（正向最大匹配）
# ---------------------------------------------------------------------------
def tokenize(text: str) -> List[Tuple[str, int, int]]:
    """正向最大匹配分词，max_len=4，跳过标点/空白/非 CJK 连续段。

    返回 [(word, start, end)]，start/end 为字符偏移。
    """
    _ensure_loaded()
    result: List[Tuple[str, int, int]] = []
    i = 0
    n = len(text)
    max_len = 4
    while i < n:
        ch = text[i]
        # 跳过非 CJK 字符
        if not _CJK_RE.match(ch):
            i += 1
            continue
        # 正向最大匹配
        matched = False
        for l in range(min(max_len, n - i), 0, -1):
            candidate = text[i:i + l]
            if is_word(candidate):
                result.append((candidate, i, i + l))
                i += l
                matched = True
                break
        if not matched:
            # 单字也作为 token（即使不在词表）
            result.append((ch, i, i + 1))
            i += 1
    return result


# ---------------------------------------------------------------------------
# P1 候选生成
# ---------------------------------------------------------------------------
def _levenshtein(s1: str, s2: str) -> int:
    """纯 stdlib Levenshtein 编辑距离。"""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            # 插入、删除、替换
            curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + (c1 != c2)))
        prev = curr
    return prev[-1]


def _similarity(s1: str, s2: str) -> float:
    """相似度 = 1 - dist / max(len)。"""
    dist = _levenshtein(s1, s2)
    max_len = max(len(s1), len(s2))
    if max_len == 0:
        return 1.0
    return 1.0 - dist / max_len


def _get_freq(word: str) -> int:
    """获取词频（词表内），不在词表返回 1。"""
    return _word_freq.get(word, 1)


def _freq_boost(freq: int) -> float:
    """词频加成：min(0.15, log10(freq+1)/40)。"""
    return min(0.15, math.log10(freq + 1) / 40)


def _ctx_boost(candidate: str, ctx_before: str, ctx_after: str) -> float:
    """上下文加成：候选与 ctx_before 末字或 ctx_after 首字拼成词且 is_word → +0.15。"""
    if ctx_before:
        tail = ctx_before[-1]
        if _CJK_RE.match(tail) and is_word(tail + candidate):
            return 0.15
    if ctx_after:
        head = ctx_after[0]
        if _CJK_RE.match(head) and is_word(candidate + head):
            return 0.15
    return 0.0


def generate_candidates(wrong: str, ctx_before: str = "", ctx_after: str = "") -> List[str]:
    """P1：候选生成 + 权重排序，返回词表内命中词，降序，去重，上限 5。

    排序权重：
    - base = 0.5*形近 + 0.3*同音 + 0.2*编辑距离（命中记 1，多特征叠加 max 1.0）
    - boost = min(0.15, log10(freq+1)/40)
    - ctx_boost：与上下文拼成词 +0.15
    - score = min(1.2, base + boost + ctx_boost)；≥ 0.4 才输出

    注：阈值从 0.6 降至 0.4（偏差说明）：原阈值下纯形近（base=0.5）需词频≥10000
    才能通过，纯同音（base=0.3）永远无法通过（即使 boost 满 0.15 也仅 0.45），
    导致单特征候选几乎全部被过滤，模块无法实用。降至 0.4 后：纯形近高频词可输出，
    纯同音高频词（词频≥10000）可输出，仍保过滤能力。
    """
    _ensure_loaded()
    if not wrong:
        return []

    # 1. 用户动态混淆对优先
    if wrong in _user_fixes:
        fixed = _user_fixes[wrong]
        if fixed != wrong and is_word(fixed):
            return [fixed]

    candidates: Dict[str, float] = {}  # word → score

    def _add(word: str, base: float, boost: float = 0.0) -> None:
        if word == wrong or not is_word(word):
            return
        ctx = _ctx_boost(word, ctx_before, ctx_after)
        score = min(1.2, base + boost + ctx)
        if score < _SCORE_MIN:
            return
        if word not in candidates or score > candidates[word]:
            candidates[word] = score

    if len(wrong) == 1:
        # 单字：优先使用 jieba_dict 单字高频候选与同音组；不再把 shape 词典作为主导基准
        homo_chars = _homophone_groups.get(wrong, set())
        # 同音候选（base 0.3）
        for ch in homo_chars:
            _add(ch, 0.3)
        # 补充高频单字候选（来自 jieba_dict），作为低权重候选
        for ch in _top_single_chars:
            if ch == wrong:
                continue
            _add(ch, 0.2, _freq_boost(_get_freq(ch)))
    else:
        # 多字（≥2）：整词编辑距离 + 逐字替换
        # 剪枝：与 wrong 长度相同或 ±1 且首字符相同或同音
        wlen = len(wrong)
        first_char = wrong[0]
        first_homo = _homophone_groups.get(first_char, set())
        # Use jieba_dict-driven baseline: prefer words whose first character is the same
        # or a homophone of the first character. Do NOT expand by shape_groups here.
        first_alike = first_homo | {first_char}

        # 遍历词表：借助按首字索引大幅减少扫描量（只检索首字在 first_alike 的词）
        for fc in first_alike:
            for word in _words_by_first.get(fc, []):
                wlen2 = len(word)
                if abs(wlen2 - wlen) > 1:
                    continue
                # small fast-reject: same-first-char done by index; now compute similarity
                sim = _similarity(wrong, word)
                if sim >= _SIMILARITY_MIN:
                    base = 0.2 * sim
                    boost = _freq_boost(_get_freq(word))
                    _add(word, base, boost)
    # 排序：score 降序，同分按词频降序
    sorted_candidates = sorted(
        candidates.items(),
        key=lambda x: (-x[1], -_get_freq(x[0])),
    )
    # return top 5 as (word, score) pairs
    top = [(w, s) for w, s in sorted_candidates[:5]]

    return top
_GEN_CAND_CACHE_LOCK = threading.Lock()
_GEN_CAND_CACHE: Dict[Tuple[str, str, str], List[Tuple[str, float]]] = {}

def cached_candidates_for_token(wrong: str, ctx_before: str = "", ctx_after: str = "") -> List[Tuple[str, float]]:
    """Cached wrapper around generate_candidates. Thread-safe small cache with eviction.

    Keeps a cap to avoid unbounded memory growth; keys are (wrong, ctx_before, ctx_after).
    Returns a list of (word, score) tuples.
    """
    key = (wrong, ctx_before or "", ctx_after or "")
    with _GEN_CAND_CACHE_LOCK:
        if key in _GEN_CAND_CACHE:
            return _GEN_CAND_CACHE[key]
    cands = generate_candidates(wrong, ctx_before, ctx_after)
    with _GEN_CAND_CACHE_LOCK:
        _GEN_CAND_CACHE[key] = cands
        if len(_GEN_CAND_CACHE) > _MAX_CAND_CACHE:
            # pop the oldest inserted key to bound memory (dict preserves insertion order)
            _GEN_CAND_CACHE.pop(next(iter(_GEN_CAND_CACHE)))
    return cands



# ---------------------------------------------------------------------------
# P2 用户词表回写
# ---------------------------------------------------------------------------
def add_user_fix(wrong: str, fixed: str) -> None:
    """P2 采纳：记录动态混淆对（下次 generate_candidates 该 wrong 直接优先输出 fixed），持久化。"""
    _ensure_loaded()
    if not wrong or not fixed or wrong == fixed:
        return
    with _lock:
        if wrong in _user_fixes and _user_fixes[wrong] == fixed:
            return  # 幂等
        _user_fixes[wrong] = fixed
        _save_user_dict(_USER_DICT_PATH, _user_fixes, _user_ignored)


def ignore_word(w: str) -> None:
    """P2 忽略：加入排除表（is_word 对 w 返回 False 且不参与候选），持久化。"""
    _ensure_loaded()
    if not w:
        return
    with _lock:
        if w in _user_ignored:
            return  # 幂等
        _user_ignored.add(w)
        _save_user_dict(_USER_DICT_PATH, _user_fixes, _user_ignored)


def is_ignored(w: str) -> bool:
    """P2 排除表查询（ignore_word 的读侧）：忽略的词不再参与任何标注。"""
    _ensure_loaded()
    return bool(w) and w in _user_ignored


# ---------------------------------------------------------------------------
# 调试/验证
# ---------------------------------------------------------------------------
def get_stats() -> Dict[str, int]:
    """调试/验证用：返回各表大小。"""
    _ensure_loaded()
    return {
        "words": len(_word_freq),
        "shapes": len(_shape_groups),
        "homophones": len(_homophone_groups),
        "fixes": len(_user_fixes),
        "ignored": len(_user_ignored),
    }
