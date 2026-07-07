"""Structured logging to both stdout and a rotating file."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(level: str = "INFO", data_dir: str = "data") -> logging.Logger:
    """Configure the root 'housing_agent' logger once. Safe to call repeatedly."""
    logger = logging.getLogger("housing_agent")
    if logger.handlers:  # already configured
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    log_dir = Path(data_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "housing_agent.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    logger.propagate = False
    return logger
