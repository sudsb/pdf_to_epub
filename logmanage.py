"""日志管理：为 ptoe 提供轮转文件日志（stdlib logging）。

- 使用 RotatingFileHandler：maxBytes=10MB，backupCount=5（总计 ≤ 60MB）。
- 日志文件路径：app_base_dir() / "ptoe.log"（程序所在目录，冻结时为 exe 目录）。
- 格式：每行以 LEVEL 开头，如 "INFO|2026-08-29 12:34:56|message"。
- 级别：默认 INFO；UTF-8 编码（Windows 兼容）。
- 仅配置文件 handler，不改动根 logger 的控制台输出（保留 print 行为）。
- 幂等：setup_logging() 多次调用只添加一次 handler（模块标志位）。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
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