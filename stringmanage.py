from zhconv import zhconv


def ttos(text):
    return zhconv.convert(text, "zh-cn")


def stot(text):
    return zhconv.convert(text, "zh-tw")


import re
from typing import Any, Dict, List


def clean_and_structure_text(
    pages: List[Dict[str, Any]],
    remove_thoughts: bool = True,
    normalize_spaces: bool = True,
    split_paragraphs: bool = True,
    merge: bool = True,
    to_simplified: bool = False,
    to_traditional: bool = False,
) -> Dict[str, Any]:
    """多页文本清洗/结构化：移除<think>标签、规范空白、繁简转换、分段，保证顺序。输入 pages: [{'page':..., 'text':...}, ...] -> 输出每页/正文合并/所有段落。"""

    assert not (to_simplified and to_traditional), "只能二选一进行简繁转换"
    pages = sorted(
        [p for p in pages if "text" in p and "page" in p], key=lambda x: x["page"]
    )
    results = []
    all_paras = []
    body_segs = []
    for orig in pages:
        t = orig["text"]
        # -- 移除<think>标签及内容 --
        if remove_thoughts:
            t = re.sub(r"<think>.*?</think>", "", t, flags=re.DOTALL | re.IGNORECASE)
        # -- 空白规范 --
        if normalize_spaces:
            t = re.sub(r"[ \u3000\t]+", " ", t)
            t = re.sub(r"[\r\n]+", "\n", t)
            t = re.sub(r"\n{2,}", "\n", t)
            t = t.strip()
        # -- 繁简字转换 -- (global disabled by default; support per-page flags)
        page_to_simplified = to_simplified or bool(orig.get("to_simplified", False))
        page_to_traditional = to_traditional or bool(orig.get("to_traditional", False))
        if page_to_simplified and not page_to_traditional:
            t = ttos(t)
        elif page_to_traditional and not page_to_simplified:
            t = stot(t)
        # -- 分割段落 --
        paras = (
            [p for p in re.split(r"(?:\n|^)[ \t\u3000]*", t) if p.strip()]
            if split_paragraphs
            else [t]
        )
        for p in paras:
            all_paras.append({"page": orig["page"], "text": p})
        results.append({"page": orig["page"], "text": t})
        body_segs.append(t)
    return {
        "pages": results,
        "body": "\n".join(body_segs) if merge else "",
        "paragraphs": all_paras,
    }
