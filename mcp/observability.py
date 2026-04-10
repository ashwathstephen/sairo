"""
Observability: structured logging, metrics tracking, and request tracing.
"""

import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

LOG_LEVEL = os.environ.get("MCP_LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.environ.get("MCP_LOG_FORMAT", "json")  # "json" or "text"


class StructuredFormatter(logging.Formatter):
    """JSON structured log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
            "logger": record.name,
        }

        # Add extra fields if present
        for key in ("tool", "user", "bucket", "duration_ms", "session_id",
                     "result_rows", "error", "status"):
            value = getattr(record, key, None)
            if value is not None:
                log_entry[key] = value

        return json.dumps(log_entry, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable log formatter for development."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        user = getattr(record, "user", "")
        tool = getattr(record, "tool", "")
        extra = ""
        if user:
            extra += f" user={user}"
        if tool:
            extra += f" tool={tool}"
        duration = getattr(record, "duration_ms", None)
        if duration is not None:
            extra += f" {duration}ms"
        return f"{ts} {record.levelname:5s} {record.getMessage()}{extra}"


def setup_logging() -> logging.Logger:
    """Configure structured logging for the MCP server."""
    logger = logging.getLogger("sairo-mcp")
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    if not logger.handlers:
        handler = logging.StreamHandler()
        if LOG_FORMAT == "json":
            handler.setFormatter(StructuredFormatter())
        else:
            handler.setFormatter(TextFormatter())
        logger.addHandler(handler)

    return logger


# Global logger instance
logger = setup_logging()


class Metrics:
    """Simple in-memory metrics for tool calls. Thread-safe via GIL for counters."""

    def __init__(self):
        self.tool_calls: dict[str, int] = {}
        self.tool_errors: dict[str, int] = {}
        self.tool_durations: dict[str, list[float]] = {}

    def record_call(self, tool: str, duration_ms: float, error: bool = False):
        """Record a tool call metric."""
        self.tool_calls[tool] = self.tool_calls.get(tool, 0) + 1
        if error:
            self.tool_errors[tool] = self.tool_errors.get(tool, 0) + 1

        if tool not in self.tool_durations:
            self.tool_durations[tool] = []
        durations = self.tool_durations[tool]
        durations.append(duration_ms)
        # Keep last 1000 durations per tool
        if len(durations) > 1000:
            self.tool_durations[tool] = durations[-1000:]

    def get_summary(self) -> dict:
        """Get metrics summary."""
        summary = {}
        for tool in sorted(self.tool_calls.keys()):
            durations = self.tool_durations.get(tool, [])
            summary[tool] = {
                "calls": self.tool_calls.get(tool, 0),
                "errors": self.tool_errors.get(tool, 0),
                "avg_ms": round(sum(durations) / len(durations), 1) if durations else 0,
                "p95_ms": round(sorted(durations)[int(len(durations) * 0.95)] if durations else 0, 1),
            }
        return summary

    def reset(self):
        """Reset all metrics."""
        self.tool_calls.clear()
        self.tool_errors.clear()
        self.tool_durations.clear()


# Global metrics instance
metrics = Metrics()


@contextmanager
def track_tool_call(
    tool_name: str,
    user: Optional[str] = None,
    bucket: Optional[str] = None,
):
    """
    Context manager that tracks tool call duration and logs the result.

    Usage:
        with track_tool_call("list_buckets", user="admin") as ctx:
            result = do_work()
            ctx["result_rows"] = len(result)
    """
    ctx = {"result_rows": None, "error": None}
    start = time.monotonic()

    try:
        yield ctx
    except Exception as e:
        ctx["error"] = str(e)
        raise
    finally:
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        is_error = ctx["error"] is not None

        metrics.record_call(tool_name, duration_ms, error=is_error)

        extra = {
            "tool": tool_name,
            "duration_ms": duration_ms,
            "status": "error" if is_error else "ok",
        }
        if user:
            extra["user"] = user
        if bucket:
            extra["bucket"] = bucket
        if ctx["result_rows"] is not None:
            extra["result_rows"] = ctx["result_rows"]
        if ctx["error"]:
            extra["error"] = ctx["error"]

        if is_error:
            logger.error(f"Tool call failed: {tool_name}", extra=extra)
        else:
            logger.info(f"Tool call: {tool_name}", extra=extra)
