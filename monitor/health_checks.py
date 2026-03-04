"""
Rout Health Check Interface — standardized service health checking.

Four check types:
  - LaunchdChecker: checks if a launchd service is running via `launchctl list`
  - HttpChecker: GET request to an endpoint, 2xx = active
  - SocketChecker: TCP connect to host:port
  - LogActivityChecker: skip PID, use log file recency only (for interval/cron services)

Three-state model:
  - active: check passes AND recent activity (within stale_threshold)
  - degraded: check passes BUT no recent activity
  - offline: check fails (or no log file found for log-activity checks)

Usage:
    checker = create_checker(service_config)
    result = checker.check()
    print(result.status, result.latency_ms)
"""

import json
import os
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class HealthStatus(Enum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass
class CheckResult:
    """Raw result from a health checker (before stale_threshold logic)."""
    alive: bool
    pid: Optional[int] = None
    latency_ms: float = 0.0
    message: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthResult:
    """Final health result with status, activity, and metadata."""
    status: HealthStatus
    pid: Optional[int] = None
    latency_ms: float = 0.0
    message: str = ""
    last_activity: Optional[float] = None  # epoch timestamp
    last_activity_age: Optional[int] = None  # seconds
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "pid": self.pid,
            "latency_ms": round(self.latency_ms, 1),
            "message": self.message,
            "last_activity": self.last_activity,
            "last_activity_age": self.last_activity_age,
            **self.extra,
        }


# === CHECKER INTERFACE ===

class HealthChecker:
    """Base class for health checks."""

    def check(self) -> CheckResult:
        raise NotImplementedError


class LaunchdChecker(HealthChecker):
    """Check if a launchd service is loaded and running."""

    def __init__(self, label: str):
        self.label = label

    def check(self) -> CheckResult:
        t0 = time.time()
        try:
            result = subprocess.run(
                ["launchctl", "list", self.label],
                capture_output=True, text=True, timeout=5
            )
            latency = (time.time() - t0) * 1000

            if result.returncode != 0:
                return CheckResult(alive=False, latency_ms=latency, message="not loaded")

            pid = self._parse_pid(result.stdout)
            if pid and pid > 0:
                return CheckResult(alive=True, pid=pid, latency_ms=latency, message="running")
            else:
                return CheckResult(alive=False, latency_ms=latency, message="loaded but not running")

        except subprocess.TimeoutExpired:
            return CheckResult(alive=False, latency_ms=5000, message="launchctl timeout")
        except Exception as e:
            return CheckResult(alive=False, message=f"error: {e}")

    @staticmethod
    def _parse_pid(output: str) -> Optional[int]:
        for line in output.strip().split("\n"):
            line = line.strip()
            if '"PID"' in line:
                parts = line.replace('"PID"', "").replace("=", "").replace(";", "").strip().split()
                if parts:
                    try:
                        return int(parts[0])
                    except ValueError:
                        pass
        return None


class HttpChecker(HealthChecker):
    """Check if an HTTP endpoint responds with 2xx."""

    def __init__(self, url: str, timeout: int = 3, parse_models: bool = False):
        self.url = url
        self.timeout = timeout
        self.parse_models = parse_models

    def check(self) -> CheckResult:
        t0 = time.time()
        try:
            req = urllib.request.Request(self.url, method="GET")
            resp = urllib.request.urlopen(req, timeout=self.timeout)
            latency = (time.time() - t0) * 1000
            body = resp.read().decode("utf-8", errors="replace")

            extra = {}
            if self.parse_models:
                try:
                    data = json.loads(body)
                    extra["models"] = [m.get("name", "unknown") for m in data.get("models", [])]
                except (json.JSONDecodeError, KeyError):
                    extra["models"] = []

            return CheckResult(
                alive=True,
                latency_ms=latency,
                message=f"HTTP {resp.getcode()}",
                extra=extra,
            )

        except urllib.error.URLError as e:
            latency = (time.time() - t0) * 1000
            return CheckResult(alive=False, latency_ms=latency, message=str(e.reason))
        except Exception as e:
            latency = (time.time() - t0) * 1000
            return CheckResult(alive=False, latency_ms=latency, message=str(e))


class SocketChecker(HealthChecker):
    """Check if a TCP port is open."""

    def __init__(self, host: str, port: int, timeout: int = 3):
        self.host = host
        self.port = port
        self.timeout = timeout

    def check(self) -> CheckResult:
        t0 = time.time()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            sock.connect((self.host, self.port))
            sock.close()
            latency = (time.time() - t0) * 1000
            return CheckResult(alive=True, latency_ms=latency, message=f"port {self.port} open")
        except (socket.timeout, ConnectionRefusedError, OSError) as e:
            latency = (time.time() - t0) * 1000
            return CheckResult(alive=False, latency_ms=latency, message=str(e))


class LogActivityChecker(HealthChecker):
    """
    For interval/cron services that run-and-exit on a schedule.

    Skips PID checking entirely. Always returns alive=True so that
    resolve_status() falls through to the activity/stale_threshold logic.
    The actual health determination comes from log file recency in run_check().

    If no log file exists, run_check() handles the OFFLINE case separately.
    """

    def __init__(self, expected_interval: int = 900):
        self.expected_interval = expected_interval

    def check(self) -> CheckResult:
        return CheckResult(
            alive=True,
            latency_ms=0.0,
            message="interval service",
            extra={"expected_interval": self.expected_interval},
        )


# === ACTIVITY TRACKER ===

class ActivityTracker:
    """Track log file modification times for stale detection."""

    def __init__(self, log_dir: str):
        self.log_dir = os.path.expanduser(log_dir)

    def get_last_activity(self, log_file: Optional[str]) -> Optional[float]:
        """Return epoch timestamp of last modification, or None."""
        if not log_file:
            return None
        path = os.path.join(self.log_dir, log_file)
        if os.path.isfile(path):
            return os.path.getmtime(path)
        return None

    def get_all_activity(self) -> Dict[str, dict]:
        """Return activity info for all log files."""
        activity = {}
        if not os.path.isdir(self.log_dir):
            return activity
        now = time.time()
        for fname in sorted(os.listdir(self.log_dir)):
            fpath = os.path.join(self.log_dir, fname)
            if os.path.isfile(fpath):
                mtime = os.path.getmtime(fpath)
                age = int(now - mtime)
                activity[fname] = {
                    "last_modified": mtime,
                    "age_seconds": age,
                    "age_human": _format_age(age),
                }
        return activity


# === RESOLVE FINAL STATUS ===

def resolve_status(
    check: CheckResult,
    last_activity: Optional[float],
    stale_threshold: int,
) -> HealthStatus:
    """
    Combine check result + activity into three-state status.

    active: check passes AND activity within threshold
    degraded: check passes BUT activity stale (or no log_file configured)
    offline: check fails
    """
    if not check.alive:
        return HealthStatus.OFFLINE

    # If no activity tracking configured, just go by check result
    if last_activity is None:
        return HealthStatus.ACTIVE

    age = time.time() - last_activity
    if age > stale_threshold:
        return HealthStatus.DEGRADED

    return HealthStatus.ACTIVE


# === FACTORY ===

def create_checker(config: dict) -> HealthChecker:
    """Create the appropriate HealthChecker from a service config dict."""
    check_type = config.get("check_type", "launchd")

    if check_type == "launchd":
        return LaunchdChecker(label=config["launchd_label"])
    elif check_type == "http":
        return HttpChecker(
            url=config["url"],
            timeout=config.get("timeout", 3),
            parse_models=config.get("parse_models", False),
        )
    elif check_type == "socket":
        return SocketChecker(
            host=config["host"],
            port=config["port"],
            timeout=config.get("timeout", 3),
        )
    elif check_type == "log-activity":
        return LogActivityChecker(
            expected_interval=config.get("expected_interval", 900),
        )
    else:
        raise ValueError(f"Unknown check_type: {check_type}")


def run_check(
    service_id: str,
    config: dict,
    activity_tracker: ActivityTracker,
) -> HealthResult:
    """Run a full health check for a service: check + activity → final status."""
    checker = create_checker(config)
    raw = checker.check()

    log_file = config.get("log_file")
    last_activity = activity_tracker.get_last_activity(log_file)
    stale_threshold = config.get("stale_threshold", 7200)
    check_type = config.get("check_type", "launchd")

    # For log-activity checks, no log file = OFFLINE (can't verify service ran)
    if check_type == "log-activity" and last_activity is None:
        return HealthResult(
            status=HealthStatus.OFFLINE,
            latency_ms=raw.latency_ms,
            message="no log file found",
            extra=raw.extra,
        )

    status = resolve_status(raw, last_activity, stale_threshold)

    last_activity_age = None
    if last_activity is not None:
        last_activity_age = int(time.time() - last_activity)

    return HealthResult(
        status=status,
        pid=raw.pid,
        latency_ms=raw.latency_ms,
        message=raw.message,
        last_activity=last_activity,
        last_activity_age=last_activity_age,
        extra=raw.extra,
    )


# === HELPERS ===

def _format_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s ago"
    elif seconds < 3600:
        return f"{seconds // 60}m ago"
    elif seconds < 86400:
        return f"{seconds // 3600}h ago"
    else:
        return f"{seconds // 86400}d ago"
