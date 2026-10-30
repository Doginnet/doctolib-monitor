"""Centralized Loguru logging configuration."""

from __future__ import annotations

import sys
from pathlib import Path
from loguru import logger

from config.settings import settings


def setup_logger(level: str = "INFO", log_dir: Path | None = None) -> None:
    target_dir = log_dir or settings.doctolib_logs_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - <level>{message}</level>",
    )
    logger.add(
        target_dir / "doctolib_monitor.log",
        level="DEBUG",
        rotation="10 MB",
        retention="14 days",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    )
