# -*- coding: utf-8 -*-
"""跨模型 OCR 截断检测脚本（2026-09-07）。

对每个选定的模型注册名：
  1) 停掉残留服务 → runserver 全新启动（parallel=1，按 config llama_server_args）
  2) 探测 /slots 的真实 n_ctx
  3) 对一张 ~800 字的密集 CJK 测试页做一次 OCR（_request_image_new）
  4) 捕获打印的截断警告 + finish_reason + usage + 实际输出字符数

另含「陈旧服务复现」阶段：手动以 --ctx-size 2048 启动 llama-server，
模拟用户环境中被复用的旧残留进程（真实 n_ctx=2048 → 触发 2048 截断）。
"""
import base64
import io
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import fitz  # noqa: E402  生成密集测试页
import llamamanage as llm  # noqa: E402

MODELS = ["glm8", "DOTS8", "ULQ8", "q34", "HY"]  # config.json model_choices 注册键
DPI = 150
PROMPT = (getattr(llm, "OCR_PROMPT", None)
          or "请逐行完整识别图片中的全部文字，逐字输出，不得遗漏任何内容、不得省略、不得总结、不得翻译")
CJK_SENT = ("本书为一部涵盖中国近现代历史的通俗读物，内容涉及政治、经济、文化与日常生活诸方面。"
            "作者以扎实的史料为基础，结合田野调查与档案文献，力图还原历史的本来面貌。"
            "全书按时间顺序分为若干章节，每章侧重一个主题时期，既照顾到宏观脉络，也关注细节刻画。"

            "第一章讲述晚清局势的变迁，分析了闭关锁国政策下的社会矛盾，以及列强叩关带来的冲击。"
            "第二章聚焦民国初年的制度探索，议会、宪法与地方自治等新事物在旧秩序中艰难生长。"
            "第三章描绘抗战年代的全民动员，从后方工业内迁到前线将士浴血奋战，皆有翔实记载。"
            "第四章讨论建国初期的社会改造，土地改革、工商业调整与思想教育同步推进。"
            "第五章回顾改革开放以来的经济腾飞，从农村联产承包到城市国企改革，从特区试验到全面开放。"
            "作者在结语中强调，历史研究不应脱离民众的日常生活，柴米油盐与宏大革命同等重要。"
            "他还提醒读者，任何时代的转型都伴随阵痛，理解历史方能理解当下。"
            "该书行文流畅，引证规范，深得学界好评，先后多次再版，深受普通读者喜爱。"
            "若有兴趣深入研究，可参阅书末附录所列的档案目录与参考书目。")


def render_dense_page() -> bytes:
    """生成一张含 ~700-900 CJK 字符的 A4 页 → PNG bytes。"""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4 pt
    paras = CJK_SENT.splitlines()
    n = 0
    lines = []
    for _ in range(6):
        for p in paras:
            if p.strip():
                lines.append(p)
    body = "\n".join(lines)
    n = sum(len(x) for x in lines)
    rect = fitz.Rect(60, 60, 535, 782)
    page.insert_textbox(rect, body, fontname="china-s", fontsize=11, align=0)
    pix = page.get_pixmap(dpi=DPI)
    data = pix.tobytes("png")
    print(f"[render] dense page computed chars={n}, png bytes={len(data)}")
    return data


def http_get_json(path: str, timeout: float = 8.0):
    with urllib.request.urlopen(f"http://127.0.0.1:8080{path}", timeout=timeout) as r:
        return json.load(r)


def probe_slots_ctx():
    """返回 /slots 首个槽位的 n_ctx；失败返回 None。"""
    try:
        slots = http_get_json("/slots")
        if isinstance(slots, list) and slots:
            return slots[0].get("n_ctx")
        return None
    except Exception as e:  # noqa: BLE001
        print(f"  [probe /slots] failed: {e!r}")
        return None


def wait_health(timeout: float = 240.0) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    return False


def stop_all():
    try:
        llm.stopserver()
    except Exception as e:  # noqa: BLE001
        print(f"  [stopserver] {e!r}")
    llm._kill_port_owner("8080")
    time.sleep(2)


def ocr_once(img_bytes: bytes, model_key: str, label: str):
    """跑一次 _request_image_new，捕获 stdout 内警告。返回 (result, error, warning)。"""
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        res = llm._request_image_new(
            PROMPT, None, model_key=model_key,
            thinking=False, img_bytes=img_bytes, content_type="image/png",
            timeout=600,
        )
    except Exception as e:  # noqa: BLE001
        res = {"result": None, "error": f"EXC {e!r}"}
    finally:
        sys.stdout = old
    warn = buf.getvalue().strip()
    out_len = len(res.get("result") or "") if isinstance(res, dict) else 0
    err = res.get("error") if isinstance(res, dict) else str(res)
    print(f"  [{label}] finish/err={err!r:.120} out_chars={out_len}")
    if warn:
        print(f"  [{label}] warn={warn!r:.220}")
    return res, err, warn


def phase_stale_reuse(img_bytes: bytes):
    """陈旧服务复现：手动以 --ctx-size 2048 启动，验证 2048 截断 + 警告分流。"""
    print("=" * 74)
    print("PHASE-STALE: 手动启动 --ctx-size 2048 服务（模拟旧残留进程）")
    stop_all()
    cfg = llm.get_config(show_dialogs=False)
    exe = cfg.get("llama_server")
    info = (cfg.get("model_choices") or {}).get("DOTS8") or {}
    mdl = os.path.join(cfg.get("models_dir") or "", info.get("name") or "")
    mmj = os.path.join(cfg.get("models_dir") or "", info.get("mmproj") or "")
    args = [exe, "-m", mdl, "--mmproj", mmj, "--ctx-size", "2048",
            "--parallel", "1", "--port", "8080", "--log-verbosity", "0",
            "--n-gpu-layers", "999"]
    print("  cmd:", " ".join(args))
    proc = subprocess.Popen(args)
    llm._server_process = proc
    if not wait_health(240):
        print("  STALE: health timeout"); proc.kill(); return
    ctx = probe_slots_ctx()
    print(f"  STALE: /slots n_ctx={ctx}")
    ocr_once(img_bytes, "DOTS8", "stale2048")
    stop_all()


def phase_models(img_bytes: bytes):
    cfg = llm.get_config(show_dialogs=False)
    choices = cfg.get("model_choices") or {}
    print("=" * 74)
    print("PHASE-MODELS: 全新启动逐模型检测")
    rows = []
    for mk in MODELS:
        print("-" * 74)
        print(f"MODEL {mk}: {choices.get(mk, {}).get('name')}")
        stop_all()
        info = choices.get(mk) or {}
        if not info.get("mmproj"):
            print(f"  {mk}: 无 mmproj（纯文本模型），跳过视觉 OCR")
            rows.append((mk, "no-mmproj", "-", "-", "-", "-"))
            continue
        try:
            ok = llm.runserver(mk, with_mmproj=True, parallel=1)
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  runserver EXC: {e!r}")
        if not ok:
            print(f"  {mk}: runserver 失败")
            rows.append((mk, "start-fail", "-", "-", "-", "-"))
            continue
        ctx = probe_slots_ctx()
        print(f"  {mk}: /slots n_ctx={ctx}")
        res, err, warn = ocr_once(img_bytes, mk, mk)
        out_len = len(res.get("result") or "") if isinstance(res, dict) else 0
        finish = "length" if warn and ("finish_reason=length" in warn) else ("ok" if err is None else "err")
        rows.append((mk, "ok", ctx, finish, out_len, warn[:80]))
        stop_all()
    print("=" * 74)
    print("SUMMARY\tmodel\tstatus\tn_ctx\tfinish\tout_chars")
    for r in rows:
        print("SUMMARY\t" + "\t".join(str(x) for x in r))


def main():
    img = render_dense_page()
    phase_stale_reuse(img)
    phase_models(img)
    stop_all()
    print("DONE")


if __name__ == "__main__":
    main()