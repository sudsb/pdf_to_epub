"""视觉校对底层能力（2026-08）：暂不接入主流程与矫正界面，供后续接入。

依赖 llamamanage（llama-server + 视觉模型）完成图片推理；本模块仅封装
校对提示词与差异比对逻辑，不直接启动/管理服务器进程。
"""

import difflib

from llamamanage import _request_image_new

# 校对提示词模板：逐字对照图片原文修正 OCR 错误，补齐遗漏。
# {text} 占位由调用方填入 OCR 识别文本。
PROOFREAD_PROMPT_TEMPLATE = (
    "请逐字对照图片中的原文，校对以下 OCR 识别文本，修正识别错误的字词，"
    "补齐遗漏的内容。只输出修正后的完整文本，不要解释、不要标注、"
    "不得省略、不得总结、不得翻译。\nOCR文本：\n{text}"
)


def proofread_page_image(image, text, *, timeout=600, thinking=False):
    """对单页图片进行视觉校对，返回修正后的文本。

    Args:
        image: 图片对象（duck-typed，只需可调用 get_base64() 返回 base64 字符串），
            与 llamamanage._request_image_new 的 img 参数兼容。
        text: OCR 识别文本（待校对的原文）。
        timeout: 单页推理超时（秒），默认 600。
        thinking: 是否启用思考链（默认 False，沿用 llamamanage 惯例）。

    Returns:
        dict: {"result": str|None, "error": str|None}
            result 为修正后的完整文本，error 为异常信息（无异常时为 None）。
    """
    prompt = PROOFREAD_PROMPT_TEMPLATE.format(text=text)
    return _request_image_new(
        prompt,
        img=image,
        thinking=thinking,
        timeout=timeout,
    )


def diff_corrections(original, corrected):
    """比对原文与纠正文本，返回差异列表（字符偏移基于 original）。

    Args:
        original: 原始 OCR 文本。
        corrected: 校对后的文本。

    Returns:
        list[dict]: 每项结构 {start, end, wrong, candidates}
            - start/end: original 中的字符偏移（original[start:end] == wrong）
            - wrong: 原文片段
            - candidates: 纠正片段列表（replace 时含一个元素，delete 时为空列表）
            insert / equal 操作被跳过。
    """
    ops = difflib.SequenceMatcher(None, original, corrected).get_opcodes()
    diffs = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "replace":
            diffs.append({
                "start": i1,
                "end": i2,
                "wrong": original[i1:i2],
                "candidates": [corrected[j1:j2]],
            })
        elif tag == "delete":
            diffs.append({
                "start": i1,
                "end": i2,
                "wrong": original[i1:i2],
                "candidates": [],
            })
        # insert / equal 跳过
    return diffs
