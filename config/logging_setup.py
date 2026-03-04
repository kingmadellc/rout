"""
Rout Logging Setup — structured logging with rotation.
========================================================
Replaces ad-hoc _log() functions and bare print() calls across the codebase.

Features:
  - JSON structured logging for machine-parseable output
  - Rotating file handler (10MB per file, 5 backups = 50MB max per component)
  - Console handler for development/debugging
  - Per-component loggers (watcher, webhook, proactive, bb_push, etc.)
  - Consistent timestamp format across all components

Usage:
    from config.logging_setup import get_logger

    logger = get_logger("webhook_server")
    logger.info("Server started", extra={"port": 7888})
    logger.error("Send failed", extra={"chat_id": 1, "error": str(e)})
"""

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


# ── Paths ─────────────────────────────────────────────────────────────────────

OPENCLAW_DIR = Path(
    os.environ.get("ROUT_OPENCLAW_DIR", str(Path.home() / ".openclaw"))
).expanduser()

LOG_DIR = OPENCLAW_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── JSON Formatter ────────────────────────────────────────────────────────────


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter.

    Output format:
        {"ts": "2026-03-01T12:00:00Z", "level": "INFO", "component": "webhook", "msg": "...", ...}
    """

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "component": record.name,
            "msg": record.getMessage(),
        }

        # Add any extra fields passed via logger.info("msg", extra={...})
        for key, val in record.__dict__.items():
            if key not in (
                "name", "msg", "args", "created", "relativeCreated",
                "exc_info", "exc_text", "stack_info", "lineno", "funcName",
                "pathname", "filename", "module", "levelno", "levelname",
                "thread", "threadName", "process", "processName",
                "msecs", "message", "taskName",
            ):
                entry[key] = val

        if record.exc_info and record.exc_info[1]:
            entry["exception"] = str(record.exc_info[1])
            entry["exception_type"] = type(record.exc_info[1]).__name__

        return json.dumps(entry, default=str)


class HumanFormatter(logging.Formatter):
    """Human-readable formatter for console output."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        level = record.levelname[0]  # I, W, E, D
        return f"[{ts}] {level} [{record.name}] {record.getMessage()}"


# ── Logger Factory ────────────────────────────────────────────────────────────

_configured_loggers: dict = {}


def get_logger(
    component: str,
    level: int = logging.INFO,
    max_bytes: int = 10 * 1024 * 1024,  # 10MB
    backup_count: int = 5,
    console: bool = True,
) -> logging.Logger:
    """Get or create a structured logger for a Rout component.

    Args:
        component: Logger name / log file prefix (e.g., "webhook_server", "bb_push")
        level: Logging level (default INFO)
        max_bytes: Max size per log file before rotation (default 10MB)
        backup_count: Number of rotated files to keep (default 5)
        console: Whether to also log to stderr (default True)

    Returns:
        Configured logging.Logger instance
    """
    if component in _configured_loggers:
        return _configured_loggers[component]

    logger = logging.getLogger(f"rout.{component}")
    logger.setLevel(level)
    logger.propagate = False  # Don't double-log via root logger

    # File handler — JSON structured, with rotation
    log_file = LOG_DIR / f"{component}.log"
    file_handler = logging.handlers.RotatingFileHandler(
        str(log_file),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(JSONFormatter())
    file_handler.setLevel(level)
    logger.addHandler(file_handler)

    # Console handler — human-readable
    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(HumanFormatter())
        console_handler.setLevel(level)
        logger.addHandler(console_handler)

    _configured_loggers[component] = logger
    return logger


def get_audit_logger(component: str) -> logging.Logger:
    """Get a separate audit logger that writes JSONL to an audit file.

    Audit logs are append-only, never rotated automatically (rotated by
    the log-rotation launchd service instead).
    """
    name = f"rout.audit.{component}"
    if name in _configured_loggers:
        return _configured_loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    audit_file = LOG_DIR / f"{component}_audit.jsonl"
    handler = logging.FileHandler(str(audit_file), encoding="utf-8")
    handler.setFormatter(JSONFormatter())
    logger.addHandler(handler)

    _configured_loggers[name] = logger
    return logger
