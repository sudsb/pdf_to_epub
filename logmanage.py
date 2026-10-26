"""日志管理：为 ptoe 提供轮转文件日志（stdlib logging）。

- 使用 RotatingFileHandler：maxBytes=10MB，backupCount=5（总计 ≤ 60MB）。
- 日志文件路径：app_base_dir() / "ptoe.log"（程序所在目录，冻结时为 exe 目录）。
- 格式：每行以 LEVEL 开头，如 "INFO|2026-08-29 12:34:56|message"。
- 级别：默认 INFO；UTF-8 编码（Windows 兼容）。
- 仅配置文件 handler，不改动根 logger 的控制台输出（保留 print 行为）。
- 幂等：setup_logging() 多次调用只添加一次 handler（模块标志位）。
- 任务日志：task_log(prefix) 在 logs/ 目录下按时间戳建独立文件，供
  「界面只显示进度条+一条消息、其余细节写本地日志」的场景使用。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

from pdfmanage import app_base_dir

_LOGGER_NAME = "ptoe"
_LOG_FILE_NAME = "ptoe.log"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_BACKUP_COUNT = 5
_LOG_LEVEL = logging.INFO
_DATE_FMT = "%Y-%m-%d %H:%M:%S"
_FMT = "%(levelname)s|%(asctime)s|%(message)s"

_setup_done = False


def setup_logging() -> logging.Logger:
    """初始化并返回配置好的 logger。

    幂等：重复调用只会返回同一 logger，不重复添加 handler。
    """
    global _setup_done
    logger = logging.getLogger(_LOGGER_NAME)

    if _setup_done:
        return logger

    logger.setLevel(_LOG_LEVEL)
    logger.propagate = False  # 不向根 logger 传播（避免控制台重复）

    log_path = app_base_dir() / _LOG_FILE_NAME
    # 确保目录存在（app_base_dir 通常已存在，但以防万一）
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(_LOG_LEVEL)
    formatter = logging.Formatter(_FMT, datefmt=_DATE_FMT)
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    _setup_done = True
    return logger


# 预创建 logger 供直接导入使用（setup_logging 会在首次使用时配置）
logger = logging.getLogger(_LOGGER_NAME)


def _ensure_setup() -> logging.Logger:
    """内部辅助：确保 logging 已初始化并返回 logger。"""
    if not _setup_done:
        return setup_logging()
    return logger


# ---------------------------------------------------------------------------
# 任务日志：logs/<prefix>-<时间戳>.log
# 用途：界面只显示「一条进度条 + 一条消息」，其余细节（转换子进程 stdout 等）
# 全部写入独立日志文件，用户可随时打开查看。
# ---------------------------------------------------------------------------

# 任务日志子目录名（位于程序目录下）
_LOG_DIR_NAME = "logs"


def logs_dir() -> Path:
    """任务日志目录：app_base_dir() / "logs"（不存在则创建）。"""
    d = app_base_dir() / _LOG_DIR_NAME
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _reserve(path: Path) -> bool:
    """原子占位创建日志文件（保证并发任务不撞名）；已存在返回 False。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return False
    except OSError:
        return True  # 建不了也先返回该路径（后续写入会静默失败）
    return True


def task_log_path(prefix: str = "task") -> Path:
    """新建一个任务日志文件路径：logs/<prefix>-YYYYmmdd-HHMMSS.log。

    prefix 里的非法文件名字符替换为下划线；同名文件已存在时追加序号。
    """
    import re as _re
    import time as _time

    safe = _re.sub(r"[^A-Za-z0-9_.-]+", "_", str(prefix or "task")).strip("._")
    if not safe:
        safe = "task"
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    base = logs_dir() / f"{safe}-{stamp}.log"
    if _reserve(base):
        _prune_task_logs(safe)
        return base
    for i in range(1, 1000):
        cand = logs_dir() / f"{safe}-{stamp}-{i}.log"
        if _reserve(cand):
            _prune_task_logs(safe)
            return cand
    fallback = logs_dir() / f"{safe}-{stamp}-{os.getpid()}.log"
    _reserve(fallback)
    return fallback


def _prune_task_logs(prefix: str, keep: int = 30) -> None:
    """清理同前缀的旧任务日志（保留最新 keep 个），避免 logs/ 无限增长。"""
    import re as _re

    safe = _re.sub(r"[^A-Za-z0-9_.-]+", "_", str(prefix or "task")).strip("._")
    if not safe:
        return
    d = logs_dir()
    try:
        files = [p for p in d.glob(f"{safe}-*.log") if p.is_file()]
    except OSError:
        return
    if len(files) <= keep:
        return
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[keep:]:
        try:
            p.unlink()
        except OSError:
            pass


class TaskLog:
    """任务日志写入器：按行追加到独立 .log 文件（线程安全，惰性打开）。

    用法::

        tl = TaskLog(task_log_path("convert"))
        tl.write("hello")
        tl.close()
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fh = None
        self._lock = threading.Lock()

    def _ensure(self):
        if self._fh is None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(self.path, "a", encoding="utf-8", errors="replace")
            except OSError:
                self._fh = None
        return self._fh

    def write(self, text: str) -> None:
        """追加一行（自动补换行）；写入失败静默忽略（不能因日志拖垮主流程）。"""
        if not text:
            return
        line = text if text.endswith("\n") else text + "\n"
        with self._lock:
            fh = self._ensure()
            if fh is None:
                return
            try:
                fh.write(line)
                fh.flush()
            except OSError:
                pass

    def close(self) -> None:
        """关闭文件句柄（幂等）。"""
        with self._lock:
            fh, self._fh = self._fh, None
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass

    def __enter__(self) -> "TaskLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# 便捷函数（可选，调用者也可直接用 logger.info(...) 等）
def log_debug(msg: str, *args, **kwargs) -> None:
    _ensure_setup().debug(msg, *args, **kwargs)


def log_info(msg: str, *args, **kwargs) -> None:
    _ensure_setup().info(msg, *args, **kwargs)


def log_warning(msg: str, *args, **kwargs) -> None:
    _ensure_setup().warning(msg, *args, **kwargs)


def log_error(msg: str, *args, **kwargs) -> None:
    _ensure_setup().error(msg, *args, **kwargs)


def log_exception(msg: str, *args, **kwargs) -> None:
    _ensure_setup().exception(msg, *args, **kwargs)