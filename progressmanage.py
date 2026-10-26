# -*- coding: utf-8 -*-
"""转换进度与预计完成时间（ETA）计算（2026-10-21）。

CLI（mian.py）与 GUI（guimanage.py）共用同一份 ETA 口径，避免两边算法漂移：

- **阶段内 ETA**：按「已完成 / 总数」与已用时间线性外推，速率用 EMA 平滑
  （避免单页快慢把数字甩来甩去）；样本不足（不足 2 项或耗时过短）时不给结论。
- **整体 ETA**：当前阶段剩余时间 + 后续阶段按阶段权重折算的预估时间。
  权重是经验值（OCR 占大头），因此整体 ETA 属于「量级正确、随进度收敛」的估计，
  界面上会随每次进度上报动态修正。
- 手动矫正阶段（correct）不可预估（等人操作），不计入整体 ETA。

模块无第三方依赖，可被 mian.py / guimanage.py 单独导入（PyInstaller 会自动收集）。
"""

import time

# 自动化流水线阶段与经验权重（手动矫正 correct 不在其中：耗时由用户决定）
PIPELINE_WEIGHTS = {
    "split": 0.05,  # PDF 分割为图片
    "ocr": 0.75,  # 文字识别（耗时主体）
    "structure": 0.05,  # 文本结构化
    "render": 0.05,  # 渲染 XHTML / 打包 EPUB
}

# 参与整体 ETA 的阶段顺序
PIPELINE_ORDER = ("split", "ocr", "structure", "render")

# 速率 EMA 系数（0~1，越小越平滑）
_ETA_ALPHA = 0.3

# 至少完成这么多项、且耗时超过这么多秒，才给出阶段 ETA（样本不足不给结论）
_ETA_MIN_ITEMS = 2
_ETA_MIN_SECONDS = 1.0


def format_duration(seconds) -> str:
    """把秒数格式化成中文时长（用于 CLI / GUI 展示）。

    >>> format_duration(0)
    '0秒'
    >>> format_duration(45)
    '45秒'
    >>> format_duration(192)
    '3分12秒'
    >>> format_duration(3720)
    '1小时02分'
    """
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "-"
    if total < 0:
        return "-"
    if total < 60:
        return f"{total}秒"
    if total < 3600:
        return f"{total // 60}分{total % 60:02d}秒"
    return f"{total // 3600}小时{(total % 3600) // 60:02d}分"


def format_clock(epoch_seconds) -> str:
    """把 epoch 秒格式化成「HH:MM」（本地时区）。非法值返回 "-"。"""
    try:
        ts = float(epoch_seconds)
    except (TypeError, ValueError):
        return "-"
    if ts <= 0:
        return "-"
    lt = time.localtime(ts)
    return f"{lt.tm_hour:02d}:{lt.tm_min:02d}"


def estimate_stage_eta(done: int, total: int, elapsed: float, rate: float = 0.0):
    """按线性外推估算当前阶段的剩余秒数；样本不足时返回 None。

    done/total：已完成/总数；elapsed：当前阶段已用秒数；rate：可选的平滑速率（项/秒）。
    """
    try:
        done = int(done or 0)
        total = int(total or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0 or done >= total:
        return 0.0 if total > 0 else None
    if rate and rate > 0:
        return max(0.0, (total - done) / float(rate))
    if done < _ETA_MIN_ITEMS or elapsed < _ETA_MIN_SECONDS:
        return None
    return max(0.0, (total - done) * (float(elapsed) / float(done)))


class EtaTracker:
    """转换进度跟踪器：维护阶段、速率与 ETA（CLI / GUI 共用）。

    用法::

        tracker = EtaTracker()
        info = tracker.update("ocr", done=3, total=10)
        info["eta_sec"], info["eta_total_sec"], info["eta_at"], info["elapsed_sec"]
    """

    def __init__(self, weights=None, alpha: float = _ETA_ALPHA):
        self.weights = dict(weights or PIPELINE_WEIGHTS)
        self.alpha = float(alpha)
        self.started = time.monotonic()
        self.stage = ""
        self.stage_started = self.started
        self.done = 0
        self.total = 0
        self.rate = 0.0
        self._last_done = 0
        self._last_time = self.started

    # -- 内部：阶段切换 ----------------------------------------------------
    def _switch_stage(self, stage: str, now: float) -> None:
        if stage != self.stage:
            self.stage = stage
            self.stage_started = now
            self._last_done = 0
            self._last_time = now
            self.rate = 0.0

    # -- 对外：上报一次进度 ------------------------------------------------
    def update(self, stage: str, done: int = 0, total: int = 0, now=None) -> dict:
        """上报一次进度，返回 ETA 信息字典（字段见模块说明）。"""
        now = time.monotonic() if now is None else float(now)
        stage = str(stage or "")
        try:
            done = int(done or 0)
            total = int(total or 0)
        except (TypeError, ValueError):
            done, total = 0, 0

        self._switch_stage(stage, now)
        self.done, self.total = done, total

        # 瞬时速率 → EMA（只在本阶段内累计）
        dt = now - self._last_time
        d_items = done - self._last_done
        if dt > 0 and d_items > 0:
            inst = d_items / dt
            self.rate = inst if self.rate <= 0 else (
                self.alpha * inst + (1 - self.alpha) * self.rate
            )
        self._last_done = done
        self._last_time = now

        elapsed = max(0.0, now - self.started)
        stage_elapsed = max(0.0, now - self.stage_started)
        eta_sec = estimate_stage_eta(done, total, stage_elapsed, self.rate)
        eta_total_sec = self._overall_eta(eta_sec, stage_elapsed, done, total)

        info = {
            "stage": stage,
            "done": done,
            "total": total,
            "elapsed_sec": round(elapsed, 1),
            "eta_sec": None if eta_sec is None else round(eta_sec, 1),
            "eta_total_sec": None if eta_total_sec is None else round(eta_total_sec, 1),
            "eta_at": None
            if eta_total_sec is None
            else round(time.time() + eta_total_sec, 1),
            "rate": round(self.rate, 4),
        }
        return info

    # -- 内部：整体 ETA ----------------------------------------------------
    def _overall_eta(self, eta_sec, stage_elapsed: float, done: int, total: int):
        """整体剩余时间 = 当前阶段剩余 + 后续阶段按权重折算的预估。"""
        stage = self.stage
        if stage in ("done", "error"):
            return 0.0
        if stage not in self.weights:
            # 手动矫正等不可预估阶段：不猜
            return None
        if eta_sec is None:
            return None
        # 当前阶段的总耗时估计 = 已用 + 剩余
        stage_total = stage_elapsed + eta_sec
        cur_w = self.weights.get(stage, 0.0)
        if cur_w <= 0:
            return eta_sec
        later_w = 0.0
        try:
            idx = PIPELINE_ORDER.index(stage)
        except ValueError:
            idx = -1
        if idx >= 0:
            for later in PIPELINE_ORDER[idx + 1:]:
                later_w += self.weights.get(later, 0.0)
        return eta_sec + (later_w / cur_w) * stage_total
