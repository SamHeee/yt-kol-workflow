#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Logging setup with API key masking."""

import sys
import logging
from datetime import datetime
from pathlib import Path


class MaskingFilter(logging.Filter):
    """Masks sensitive strings (API keys) in log records."""

    def __init__(self, secrets: list = None):
        super().__init__()
        self.secrets = secrets or []

    def add_secret(self, secret: str):
        if secret and len(secret) > 8 and secret not in self.secrets:
            self.secrets.append(secret)

    def filter(self, record):
        msg = record.getMessage()
        for secret in self.secrets:
            if secret in msg:
                masked = f"{secret[:8]}...{secret[-4:]}"
                record.msg = str(record.msg).replace(secret, masked)
                if record.args:
                    record.args = tuple(
                        str(a).replace(secret, masked) if isinstance(a, str) else a
                        for a in record.args
                    )
        return True


def setup_logger(
    log_dir: str = "./output/logs",
    log_level: str = "INFO",
    secrets: list = None,
) -> logging.Logger:
    """Create and configure the application logger."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_path / f"kol_workflow_{timestamp}.log"

    logger = logging.getLogger("kol_workflow")
    logger.setLevel(logging.DEBUG)  # capture everything, handlers filter

    # Remove existing handlers
    logger.handlers.clear()

    # File handler - always DEBUG
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    ))

    # Console handler - user-specified level
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    # Masking filter
    mask_filter = MaskingFilter(secrets or [])
    fh.addFilter(mask_filter)
    ch.addFilter(mask_filter)

    logger.addHandler(fh)
    logger.addHandler(ch)

    logger.info(f"日志文件: {log_file}")
    return logger


def _to_rfc3339_boundary(value: str, end_of_day: bool = False) -> str:
    """Convert YYYY-MM-DD to RFC3339 boundary datetime."""
    value = (value or "").strip()
    if not value:
        return ""
    if "T" in value:
        return value
    suffix = "23:59:59Z" if end_of_day else "00:00:00Z"
    return f"{value}T{suffix}"
