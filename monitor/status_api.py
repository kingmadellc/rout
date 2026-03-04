#!/usr/bin/env python3
"""
Rout System Monitor — Status API Server v2.0

Registry-driven health check server with state history, message throughput,
state transition tracking, skill agent scanning, proactive trigger tracking,
CPU/memory gauges, uptime streak, and hourly message histogram.

Endpoints:
    GET /              → Dashboard HTML
    GET /api/status    → Full system status JSON
    GET /api/services  → Service definitions from registry
    GET /api/history   → State history for all services (sparklines)
    GET /health        → Simple "ok" health check

Usage:
    python3 monitor/status_api.py

Config:
    config/service_registry.yaml — defines all monitored services
"""

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread, Lock
from typing import Any, Dict, List, Optional, Tuple

# Add parent dir to path so we can import monitor.health_checks
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitor.health_checks import (
    ActivityTracker,
    HealthResult,
    HealthStatus,
    run_check,
)

# === CONFIG ===
STATUS_PORT = 7890
BIND_HOST = "127.0.0.1"
CHECK_INTERVAL = 10        # seconds between health checks
HISTORY_MAX = 144          # ~24h at 10-min resolution
HISTORY_DOWNSAMPLE = 6     # store every 6th check (60s intervals from 10s checks)
TRANSITION_MAX = 50        # max state transitions to keep
LOG_DIR = os.path.expanduser("~/.openclaw/logs")
AGENT_LOG_DIR = os.path.expanduser("~/Library/Logs")
REGISTRY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "service_registry.yaml"
)
DASHBOARD_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "dashboard.html"
)
STATE_DIR = os.path.expanduser("~/.openclaw/state")
TOKEN_PATH = os.path.join(STATE_DIR, "status_api_token")


# === TOKEN MANAGEMENT ===

def _get_or_generate_token() -> str:
    """
    Read token from ROUT_STATUS_TOKEN env var or ~/.openclaw/state/status_api_token.
    If no token exists, generate one and write to state file.
    """
    # Check environment variable first
    env_token = os.environ.get("ROUT_STATUS_TOKEN")
    if env_token:
        return env_token

    # Check state file
    if os.path.isfile(TOKEN_PATH):
        try:
            with open(TOKEN_PATH, "r") as f:
                token = f.read().strip()
                if token:
                    return token
        except Exception:
            pass

    # Generate new token (32 random hex bytes)
    import secrets
    token = secrets.token_hex(32)

    # Write to state file
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(TOKEN_PATH, "w") as f:
            f.write(token)
        os.chmod(TOKEN_PATH, 0o600)  # Restrict to owner only
    except Exception as e:
        print(f"[WARNING] Could not write token to {TOKEN_PATH}: {e}", file=sys.stderr)

    return token


# === RATE LIMITING ===

class RateLimiter:
    """Per-IP rate limit: max 30 requests per minute."""

    def __init__(self, max_requests: int = 30, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests: Dict[str, deque] = {}
        self.lock = Lock()

    def is_allowed(self, ip: str) -> bool:
        """Check if IP is allowed. Returns False if rate limit exceeded."""
        now = time.time()

        with self.lock:
            if ip not in self.requests:
                self.requests[ip] = deque()

            req_deque = self.requests[ip]

            # Remove old timestamps outside the window
            while req_deque and req_deque[0] < now - self.window_seconds:
                req_deque.popleft()

            # Check limit
            if len(req_deque) >= self.max_requests:
                return False

            # Record this request
            req_deque.append(now)
            return True

    def cleanup(self):
        """Remove stale IPs from tracking (call periodically)."""
        now = time.time()
        with self.lock:
            stale_ips = [
                ip for ip, req_deque in self.requests.items()
                if not req_deque or req_deque[-1] < now - self.window_seconds
            ]
            for ip in stale_ips:
                del self.requests[ip]


# === YAML PARSER (no PyYAML dependency) ===

def parse_simple_yaml(path: str) -> dict:
    """
    Minimal YAML parser for service_registry.yaml.
    Handles flat key-value pairs nested one level under service IDs.
    """
    result = {"services": {}}
    current_service = None

    with open(path, "r") as f:
        for line in f:
            stripped = line.rstrip()
            if not stripped or stripped.lstrip().startswith("#"):
                continue
            if stripped == "services:":
                continue
            if stripped.startswith("  ") and not stripped.startswith("    "):
                key = stripped.strip().rstrip(":")
                current_service = key
                result["services"][key] = {}
                continue
            if stripped.startswith("    ") and current_service:
                parts = stripped.strip().split(":", 1)
                if len(parts) == 2:
                    k = parts[0].strip()
                    v = parts[1].strip().strip('"').strip("'")
                    if v.lower() == "true":
                        v = True
                    elif v.lower() == "false":
                        v = False
                    else:
                        try:
                            v = int(v)
                        except ValueError:
                            try:
                                v = float(v)
                            except ValueError:
                                pass
                    result["services"][current_service][k] = v

    return result


# === STATE MANAGEMENT ===

class StateHistory:
    """Rolling buffer of health states per service for sparklines."""

    def __init__(self, max_entries: int = HISTORY_MAX):
        self.max_entries = max_entries
        self.history: Dict[str, deque] = {}

    def append(self, service_id: str, status: str, timestamp: float):
        if service_id not in self.history:
            self.history[service_id] = deque(maxlen=self.max_entries)
        self.history[service_id].append({
            "status": status,
            "timestamp": timestamp,
        })

    def get(self, service_id: str) -> List[dict]:
        return list(self.history.get(service_id, []))

    def get_all(self) -> Dict[str, List[dict]]:
        return {k: list(v) for k, v in self.history.items()}


class TransitionTracker:
    """Track status changes for alerting."""

    def __init__(self, max_entries: int = TRANSITION_MAX):
        self.transitions: deque = deque(maxlen=max_entries)
        self.previous_states: Dict[str, str] = {}

    def check(self, service_id: str, new_status: str, display_name: str):
        old_status = self.previous_states.get(service_id)
        if old_status and old_status != new_status:
            self.transitions.append({
                "service_id": service_id,
                "display_name": display_name,
                "from_status": old_status,
                "to_status": new_status,
                "timestamp": time.time(),
                "time_str": datetime.now().strftime("%H:%M:%S"),
            })
        self.previous_states[service_id] = new_status

    def get_recent(self, count: int = 10) -> List[dict]:
        return list(self.transitions)[-count:]


class UptimeStreak:
    """Track how long all services have been in active state."""

    def __init__(self):
        self.streak_start: Optional[float] = None
        self.all_up = False

    def update(self, services: Dict[str, dict]):
        """Check if all agent services are active."""
        agent_statuses = [
            s.get("status", "unknown")
            for s in services.values()
            if s.get("category") == "agent"
        ]

        if not agent_statuses:
            return

        currently_all_up = all(s == "active" for s in agent_statuses)

        if currently_all_up and not self.all_up:
            # Just went all-up
            self.streak_start = time.time()
            self.all_up = True
        elif not currently_all_up:
            self.streak_start = None
            self.all_up = False

    def to_dict(self) -> dict:
        if not self.streak_start:
            return {"seconds": 0, "human": "0s", "since": None}
        elapsed = int(time.time() - self.streak_start)
        return {
            "seconds": elapsed,
            "human": _format_duration(elapsed),
            "since": datetime.fromtimestamp(self.streak_start).strftime("%Y-%m-%dT%H:%M:%S"),
        }


class MessageCounter:
    """Count messages processed today from the watcher log, with hourly histogram."""

    def __init__(self, log_path: str):
        self.log_path = log_path
        self.messages_today = 0
        self.last_message_time: Optional[float] = None
        self.hourly: List[int] = [0] * 24

    def update(self):
        if not os.path.isfile(self.log_path):
            return

        today = datetime.now().strftime("%Y-%m-%d")
        count = 0
        last_ts = None
        hourly = [0] * 24

        try:
            with open(self.log_path, "r", errors="replace") as f:
                for line in f:
                    if today in line:
                        if any(kw in line.lower() for kw in
                               ["received", "sent", "incoming", "outgoing",
                                "new message", "reply", "response"]):
                            count += 1
                            # Try to extract hour
                            match = re.search(
                                r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})', line
                            )
                            if match:
                                try:
                                    ts = datetime.fromisoformat(
                                        match.group(1).replace(" ", "T")
                                    )
                                    last_ts = ts.timestamp()
                                    hourly[ts.hour] += 1
                                except ValueError:
                                    pass
        except Exception:
            pass

        self.messages_today = count
        self.hourly = hourly
        if last_ts:
            self.last_message_time = last_ts

    def to_dict(self) -> dict:
        result = {
            "messages_today": self.messages_today,
            "hourly": self.hourly,
        }
        if self.last_message_time:
            age = int(time.time() - self.last_message_time)
            result["last_message_time"] = self.last_message_time
            result["last_message_age"] = age
            result["last_message_human"] = _format_age(age)
        return result


class SkillAgentScanner:
    """Scan skill agent log files AND agent-activity.jsonl for last run info."""

    AGENTS = {
        "scaffolder": {
            "display_name": "Scaffolder",
            "log_pattern": "rout-agent-scaffolder.log",
            "alt_patterns": ["rout-agent-integration-scaffolder.log"],
        },
        "pipeline": {
            "display_name": "Pipeline",
            "log_pattern": "rout-agent-pipeline.log",
            "alt_patterns": ["rout-agent-pipeline-specialist.log"],
        },
        "deploy": {
            "display_name": "Deploy Ops",
            "log_pattern": "rout-agent-deploy.log",
            "alt_patterns": ["rout-agent-deploy-ops.log", "rout-agent-deploy-verify.log"],
        },
        "debug": {
            "display_name": "Debug Tracer",
            "log_pattern": "rout-agent-debug.log",
            "alt_patterns": ["rout-agent-debug-tracer.log", "rout-agent-health-check.log"],
        },
        "pulse": {
            "display_name": "Pulse Check",
            "log_pattern": "rout-agent-pulse.log",
            "alt_patterns": ["rout-agent-pulse-check.log"],
        },
        "logs": {
            "display_name": "Log Scanner",
            "log_pattern": "rout-agent-logs.log",
            "alt_patterns": ["rout-agent-log-scanner.log"],
        },
        "orchestrator": {
            "display_name": "Pipeline Orchestrator",
            "log_pattern": "rout-agent-orchestrator.log",
            "alt_patterns": ["rout-agent-pipeline-orchestrator.log"],
        },
    }

    # Also check scheduled agent logs
    SCHEDULED_LOGS = {
        "debug": "rout-agent-health-check.log",
        "deploy": "rout-agent-deploy-verify.log",
    }

    ACTIVITY_FILE = os.path.expanduser("~/.openclaw/logs/agent-activity.jsonl")

    def __init__(self, log_dir: str):
        self.log_dir = log_dir

    def scan(self) -> Dict[str, dict]:
        results = {}
        now = time.time()
        today = datetime.now().strftime("%Y-%m-%d")

        # First, read the activity JSONL file for live breadcrumbs
        activity_data = self._read_activity_file(now, today)

        for agent_id, config in self.AGENTS.items():
            # Try primary pattern first, then alternates
            log_file = os.path.join(self.log_dir, config["log_pattern"])
            if not os.path.isfile(log_file):
                for alt in config.get("alt_patterns", []):
                    alt_path = os.path.join(self.log_dir, alt)
                    if os.path.isfile(alt_path):
                        log_file = alt_path
                        break

            # Also check scheduled logs for this agent type
            sched_log = self.SCHEDULED_LOGS.get(agent_id)
            sched_file = os.path.join(self.log_dir, sched_log) if sched_log else None

            info = {
                "display_name": config["display_name"],
                "last_run_time": None,
                "last_run_human": None,
                "last_prompt": None,
                "last_status": "idle",
                "last_source": None,
                "runs_today": 0,
                "live_session": None,
            }

            # Parse the main log file
            self._parse_log(log_file, info, now, today)

            # Check scheduled log too (may be more recent)
            if sched_file:
                sched_info = dict(info)
                self._parse_log(sched_file, sched_info, now, today)
                # Use whichever has a more recent run
                if (sched_info["last_run_time"] or 0) > (info["last_run_time"] or 0):
                    info["last_run_time"] = sched_info["last_run_time"]
                    info["last_run_human"] = sched_info["last_run_human"]
                    info["last_prompt"] = sched_info["last_prompt"]
                    info["last_status"] = sched_info["last_status"]
                info["runs_today"] += sched_info["runs_today"]

            # Merge activity JSONL data — this takes priority if more recent
            if agent_id in activity_data:
                act = activity_data[agent_id]

                # Activity breadcrumbs always override if more recent
                if (act["last_epoch"] or 0) > (info["last_run_time"] or 0):
                    info["last_run_time"] = act["last_epoch"]
                    info["last_run_human"] = _format_age(int(now - act["last_epoch"])) if act["last_epoch"] else None
                    info["last_prompt"] = act["last_prompt"]
                    info["last_status"] = act["last_status"]
                    info["last_source"] = act["last_source"]

                # If there's a live "running" session, always show it
                if act["live_session"]:
                    info["live_session"] = act["live_session"]
                    info["last_status"] = "running"

                # Add activity runs_today to log-based count (dedupe: activity should be primary)
                info["runs_today"] = max(info["runs_today"], act["runs_today"])

            results[agent_id] = info

        return results

    def _read_activity_file(self, now: float, today: str) -> Dict[str, dict]:
        """
        Read agent-activity.jsonl and aggregate per-agent info.
        Returns dict keyed by canonical agent name.
        """
        data: Dict[str, dict] = {}

        if not os.path.isfile(self.ACTIVITY_FILE):
            return data

        try:
            with open(self.ACTIVITY_FILE, "r", errors="replace") as f:
                lines = f.readlines()
        except Exception:
            return data

        # Track sessions to detect "running" without "completed"
        sessions: Dict[str, dict] = {}  # session_id → latest entry

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            agent = entry.get("agent", "")
            status = entry.get("status", "")
            epoch = entry.get("epoch", 0)
            session_id = entry.get("session_id", "")
            prompt = entry.get("prompt")
            source = entry.get("source", "unknown")

            if not agent:
                continue

            # Track this session
            if session_id:
                sessions[session_id] = entry

            # Initialize agent data
            if agent not in data:
                data[agent] = {
                    "last_epoch": None,
                    "last_prompt": None,
                    "last_status": "idle",
                    "last_source": None,
                    "runs_today": 0,
                    "live_session": None,
                }

            d = data[agent]

            # Update last run info if this entry is more recent
            if epoch and (d["last_epoch"] is None or epoch > d["last_epoch"]):
                d["last_epoch"] = epoch
                d["last_status"] = status
                d["last_source"] = source
                if prompt:
                    d["last_prompt"] = prompt

            # Count today's runs (only count terminal statuses to avoid double-counting)
            if status in ("completed", "failed", "timeout"):
                ts_str = entry.get("timestamp", "")
                if today in ts_str:
                    d["runs_today"] += 1

        # Find live sessions: "running" entries with no matching "completed/failed/timeout"
        # within the last 30 minutes (stale = probably crashed)
        completed_sessions = set()
        for sid, entry in sessions.items():
            if entry.get("status") in ("completed", "failed", "timeout"):
                completed_sessions.add(sid)

        for sid, entry in sessions.items():
            if (entry.get("status") == "running"
                    and sid not in completed_sessions
                    and entry.get("epoch", 0) > now - 1800):  # 30 min staleness
                agent = entry.get("agent", "")
                if agent in data:
                    data[agent]["live_session"] = {
                        "session_id": sid,
                        "prompt": entry.get("prompt"),
                        "source": entry.get("source", "unknown"),
                        "started_epoch": entry.get("epoch"),
                        "started_human": _format_age(int(now - entry.get("epoch", now))),
                        "running_for": int(now - entry.get("epoch", now)),
                    }

        return data

    def _parse_log(self, log_file: str, info: dict, now: float, today: str):
        """Parse a rout-agent log file for run information."""
        if not os.path.isfile(log_file):
            return

        try:
            with open(log_file, "r", errors="replace") as f:
                content = f.read()
        except Exception:
            return

        # Find all run blocks (separated by ===...===)
        blocks = content.split("=" * 60)
        runs_today = 0
        last_time = None
        last_prompt = None

        for block in blocks:
            block = block.strip()
            if not block:
                continue

            # Extract timestamp
            time_match = re.search(r'Time:\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', block)
            if time_match:
                try:
                    ts = datetime.fromisoformat(time_match.group(1))
                    ts_epoch = ts.timestamp()

                    if today in time_match.group(1):
                        runs_today += 1

                    if last_time is None or ts_epoch > last_time:
                        last_time = ts_epoch

                        # Extract prompt
                        prompt_match = re.search(r'Prompt:\s*(.+)', block)
                        if prompt_match:
                            last_prompt = prompt_match.group(1).strip()
                except ValueError:
                    pass

        if last_time:
            age = int(now - last_time)
            info["last_run_time"] = last_time
            info["last_run_human"] = _format_age(age)
            info["last_prompt"] = last_prompt
            # If the run was recent (within 5 min), it might still be running
            # Otherwise, assume success (log exists = completed)
            if age < 300:
                info["last_status"] = "running"
            else:
                info["last_status"] = "success"

        info["runs_today"] += runs_today


class ProactiveTriggerScanner:
    """Scan proactive agent log for trigger fire times."""

    # Known triggers — aligned with proactive_triggers.yaml
    TRIGGERS = {
        "morning_briefing": {"display_name": "Morning Brief", "keywords": ["morning.brief", "daily.brief", "brief.trigger", "morning_briefing"]},
        "meeting_reminders": {"display_name": "Meetings", "keywords": ["meeting", "reminder", "lookahead", "calendar.event"]},
        "portfolio_drift": {"display_name": "Portfolio Drift", "keywords": ["portfolio", "drift", "threshold", "position.move"]},
        "calendar_conflicts": {"display_name": "Cal Conflicts", "keywords": ["conflict", "overlap", "calendar.conflict", "tomorrow"]},
        "cross_platform": {"display_name": "Cross-Platform", "keywords": ["cross.platform", "cross_platform", "divergence", "comparator", "kalshi.vs"]},
        "x_signals": {"display_name": "X Signals", "keywords": ["x.signal", "x_signal", "ddg", "brave", "signal.scan", "twitter"]},
        "edge_engine": {"display_name": "Edge Engine", "keywords": ["edge.engine", "edge_engine", "polygon", "mispricing", "probability", "qwen.analysis"]},
        "personality": {"display_name": "Personality", "keywords": ["[personality]", "personality", "editorial", "micro-initiation", "silence.message", "back-reference", "engagement_mod"]},
    }

    def __init__(self, log_dir: str):
        self.log_dir = log_dir

    def scan(self) -> Dict[str, dict]:
        results = {}
        now = time.time()

        # Try to find the proactive agent log
        proactive_log = None
        for fname in ["proactive_agent.log", "proactive-agent.log", "proactive.log"]:
            path = os.path.join(self.log_dir, fname)
            if os.path.isfile(path):
                proactive_log = path
                break

        if not proactive_log:
            # Return default entries with no data
            for tid, tcfg in self.TRIGGERS.items():
                results[tid] = {
                    "display_name": tcfg["display_name"],
                    "last_fired": None,
                    "last_fired_human": None,
                    "produced_output": False,
                }
            return results

        # Read last ~500 lines of log
        try:
            with open(proactive_log, "r", errors="replace") as f:
                lines = f.readlines()
            recent_lines = lines[-500:] if len(lines) > 500 else lines
        except Exception:
            recent_lines = []

        for tid, tcfg in self.TRIGGERS.items():
            last_fired = None
            produced_output = False

            for line in reversed(recent_lines):
                lower = line.lower()
                if any(kw in lower for kw in tcfg["keywords"]):
                    # Found a trigger reference — extract timestamp
                    ts_match = re.search(
                        r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})', line
                    )
                    if ts_match:
                        try:
                            ts = datetime.fromisoformat(
                                ts_match.group(1).replace(" ", "T")
                            )
                            last_fired = ts.timestamp()
                            # Check if it produced output (look for "sent", "alert", "notify")
                            if any(w in lower for w in ["sent", "alert", "notif", "deliver", "output"]):
                                produced_output = True
                            break
                        except ValueError:
                            pass

            info = {
                "display_name": tcfg["display_name"],
                "last_fired": last_fired,
                "last_fired_human": _format_age(int(now - last_fired)) if last_fired else None,
                "produced_output": produced_output,
            }
            results[tid] = info

        return results


# === SYSTEM INFO ===

def get_system_info() -> dict:
    info = {}

    # Uptime
    try:
        uptime_raw = subprocess.run(
            ["uptime"], capture_output=True, text=True, timeout=5
        )
        info["uptime_raw"] = uptime_raw.stdout.strip()
        match = re.search(r"up\s+(.+?),\s+\d+\s+user", uptime_raw.stdout)
        if match:
            info["uptime"] = match.group(1).strip()
    except Exception:
        info["uptime"] = "unknown"

    # Load average
    try:
        load = subprocess.run(
            ["sysctl", "-n", "vm.loadavg"],
            capture_output=True, text=True, timeout=5
        )
        info["load"] = load.stdout.strip().strip("{ }").strip()
    except Exception:
        info["load"] = "unknown"

    # Disk usage
    try:
        disk = subprocess.run(
            ["df", "-h", "/"], capture_output=True, text=True, timeout=5
        )
        lines = disk.stdout.strip().split("\n")
        if len(lines) > 1:
            parts = lines[1].split()
            info["disk"] = {
                "total": parts[1],
                "used": parts[2],
                "avail": parts[3],
                "pct": parts[4],
            }
    except Exception:
        info["disk"] = {}

    # CPU usage (from top)
    try:
        top = subprocess.run(
            ["top", "-l", "1", "-n", "0", "-s", "0"],
            capture_output=True, text=True, timeout=10
        )
        for line in top.stdout.split("\n"):
            if "CPU usage" in line:
                # CPU usage: 12.34% user, 5.67% sys, 81.99% idle
                user_match = re.search(r'(\d+\.?\d*)%\s*user', line)
                sys_match = re.search(r'(\d+\.?\d*)%\s*sys', line)
                idle_match = re.search(r'(\d+\.?\d*)%\s*idle', line)
                if user_match:
                    info["cpu_user"] = str(int(float(user_match.group(1))))
                if sys_match:
                    info["cpu_sys"] = str(int(float(sys_match.group(1))))
                if idle_match:
                    info["cpu_idle"] = str(int(float(idle_match.group(1))))
                break
    except Exception:
        pass

    # Memory usage
    try:
        # Total physical memory
        mem_total = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True, timeout=5
        )
        total_bytes = int(mem_total.stdout.strip())
        total_gb = total_bytes / (1024 ** 3)

        # Parse vm_stat for used memory
        vm = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5
        )
        page_size = 16384  # Default macOS page size
        ps_match = re.search(r'page size of (\d+) bytes', vm.stdout)
        if ps_match:
            page_size = int(ps_match.group(1))

        pages = {}
        for line in vm.stdout.split("\n"):
            match = re.match(r'(.+?):\s+(\d+)', line)
            if match:
                key = match.group(1).strip().lower()
                pages[key] = int(match.group(2))

        # Active + wired + compressed ≈ "used"
        active = pages.get("pages active", 0)
        wired = pages.get("pages wired down", 0)
        compressed = pages.get("pages occupied by compressor", 0)
        used_bytes = (active + wired + compressed) * page_size
        used_gb = used_bytes / (1024 ** 3)
        pct = int((used_gb / total_gb) * 100) if total_gb > 0 else 0

        info["memory"] = {
            "total": f"{total_gb:.1f}G",
            "used": f"{used_gb:.1f}G",
            "pct": f"{pct}%",
        }
    except Exception:
        pass

    return info


# === MAIN SERVER STATE ===

class MonitorState:
    """Central state container — thread-safe."""

    def __init__(self, registry: dict):
        self.lock = Lock()
        self.registry = registry
        self.services: Dict[str, dict] = {}
        self.system: dict = {}
        self.activity_tracker = ActivityTracker(LOG_DIR)
        self.history = StateHistory()
        self.transitions = TransitionTracker()
        self.uptime_streak = UptimeStreak()
        self.skill_scanner = SkillAgentScanner(AGENT_LOG_DIR)
        self.trigger_scanner = ProactiveTriggerScanner(LOG_DIR)
        self.check_count = 0

        # Message counter for the watcher log
        watcher_log = None
        watcher_cfg = registry.get("services", {}).get("imsg-watcher", {})
        if watcher_cfg.get("log_file"):
            watcher_log = os.path.join(LOG_DIR, watcher_cfg["log_file"])
        self.message_counter = MessageCounter(watcher_log or "")

    def run_all_checks(self):
        """Run health checks for all registered services."""
        services_cfg = self.registry.get("services", {})
        results = {}

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {}
            for sid, cfg in services_cfg.items():
                futures[pool.submit(run_check, sid, cfg, self.activity_tracker)] = sid

            for future in as_completed(futures, timeout=15):
                sid = futures[future]
                try:
                    result: HealthResult = future.result(timeout=10)
                    cfg = services_cfg[sid]
                    category = cfg.get("category", "agent")
                    working_threshold = cfg.get("working_threshold", 30)
                    is_working = False
                    activity_age = result.last_activity_age
                    if activity_age is not None and activity_age <= working_threshold:
                        is_working = True
                    results[sid] = {
                        "id": sid,
                        "display_name": cfg.get("display_name", sid),
                        "bot_id": cfg.get("bot_id", sid),
                        "category": category,
                        "is_working": is_working,
                        **result.to_dict(),
                    }
                except Exception as e:
                    cfg = services_cfg.get(sid, {})
                    results[sid] = {
                        "id": sid,
                        "display_name": cfg.get("display_name", sid),
                        "bot_id": cfg.get("bot_id", sid),
                        "category": cfg.get("category", "agent"),
                        "is_working": False,
                        "status": "offline",
                        "message": f"check error: {e}",
                        "latency_ms": 0,
                    }

        system = get_system_info()
        self.message_counter.update()
        all_activity = self.activity_tracker.get_all_activity()

        # Scan skill agents (every 6th check = ~1/min to avoid excess IO)
        skill_agents = {}
        proactive_triggers = {}

        now = time.time()

        with self.lock:
            self.services = results
            self.system = system
            self.check_count += 1

            # State history (downsample: every 6th check ~ 1 per minute)
            if self.check_count % HISTORY_DOWNSAMPLE == 0:
                for sid, data in results.items():
                    self.history.append(sid, data.get("status", "unknown"), now)

            # Transition tracking
            for sid, data in results.items():
                cfg = services_cfg.get(sid, {})
                self.transitions.check(
                    sid,
                    data.get("status", "unknown"),
                    cfg.get("display_name", sid),
                )

            # Uptime streak
            self.uptime_streak.update(results)

        # Skill agent scan (first check + every 6th check to reduce IO)
        if self.check_count <= 1 or self.check_count % HISTORY_DOWNSAMPLE == 0:
            try:
                skill_agents = self.skill_scanner.scan()
            except Exception as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Skill scan error: {e}",
                      file=sys.stderr)

            try:
                proactive_triggers = self.trigger_scanner.scan()
            except Exception as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Trigger scan error: {e}",
                      file=sys.stderr)

            with self.lock:
                self._cached_skill_agents = skill_agents
                self._cached_proactive_triggers = proactive_triggers

    def get_status(self) -> dict:
        with self.lock:
            return {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "epoch": time.time(),
                "services": self.services,
                "system": self.system,
                "activity": self.message_counter.to_dict(),
                "log_files": self.activity_tracker.get_all_activity(),
                "transitions": self.transitions.get_recent(10),
                "uptime_streak": self.uptime_streak.to_dict(),
                "skill_agents": getattr(self, "_cached_skill_agents", {}),
                "proactive_triggers": getattr(self, "_cached_proactive_triggers", {}),
            }

    def get_services_meta(self) -> dict:
        services_cfg = self.registry.get("services", {})
        meta = {}
        for sid, cfg in services_cfg.items():
            meta[sid] = {
                "id": sid,
                "display_name": cfg.get("display_name", sid),
                "bot_id": cfg.get("bot_id", sid),
                "category": cfg.get("category", "agent"),
            }
        return {"services": meta}

    def get_history(self) -> dict:
        with self.lock:
            return {"history": self.history.get_all()}


# === CHECK LOOP ===

def check_loop(state: MonitorState, rate_limiter: RateLimiter):
    """Background thread: run checks on interval. Also cleans up rate limiter."""
    cleanup_counter = 0
    while True:
        try:
            state.run_all_checks()
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Check error: {e}",
                  file=sys.stderr)

        # Cleanup rate limiter every 10 checks (~100 seconds)
        cleanup_counter += 1
        if cleanup_counter >= 10:
            try:
                rate_limiter.cleanup()
            except Exception as e:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Rate limiter cleanup error: {e}",
                      file=sys.stderr)
            cleanup_counter = 0

        time.sleep(CHECK_INTERVAL)


# === HTTP HANDLER ===

class StatusHandler(BaseHTTPRequestHandler):
    """Serve dashboard HTML and JSON API."""

    state: MonitorState = None
    dashboard_html: str = ""
    api_token: str = ""
    rate_limiter: RateLimiter = None

    def do_GET(self):
        # Check rate limit first
        client_ip = self.client_address[0]
        if not self.rate_limiter.is_allowed(client_ip):
            self.send_error(429, "Too Many Requests")
            return

        # Check auth for API endpoints (skip for localhost — dashboard uses same-origin fetch)
        if self.path.startswith("/api/"):
            if client_ip not in ("127.0.0.1", "::1") and not self._check_auth():
                self.send_error(401, "Unauthorized")
                return
        if self.path == "/" or self.path == "/index.html":
            self._serve_html()
        elif self.path == "/api/status":
            self._serve_json(self.state.get_status())
        elif self.path == "/api/services":
            self._serve_json(self.state.get_services_meta())
        elif self.path == "/api/history":
            self._serve_json(self.state.get_history())
        elif self.path == "/health":
            self._serve_text("ok")
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def _serve_html(self):
        payload = self.dashboard_html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_json(self, data: dict):
        payload = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_text(self, text: str):
        payload = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

    def _check_auth(self) -> bool:
        """Validate Authorization: Bearer <token> header. Use constant-time comparison."""
        auth_header = self.headers.get("Authorization", "")

        if not auth_header.startswith("Bearer "):
            return False

        provided_token = auth_header[7:]  # Strip "Bearer "

        # Use hmac.compare_digest for constant-time comparison (prevents timing attacks)
        return hmac.compare_digest(provided_token, self.api_token)

    def log_message(self, format, *args):
        pass  # Suppress per-request logging


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


def _format_duration(seconds: int) -> str:
    """Format a duration like '2d 5h 23m' or '47h 23m'."""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m"
    elif seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h {m}m"
    else:
        d = seconds // 86400
        h = (seconds % 86400) // 3600
        return f"{d}d {h}h"


def load_dashboard(path: str) -> str:
    if os.path.isfile(path):
        with open(path, "r") as f:
            return f.read()
    return "<html><body><h1>Dashboard not found</h1><p>Expected: {}</p></body></html>".format(path)


# === MAIN ===

def main():
    if not os.path.isfile(REGISTRY_PATH):
        print(f"[ERROR] Service registry not found: {REGISTRY_PATH}")
        print(f"[ERROR] Create config/service_registry.yaml first.")
        sys.exit(1)

    registry = parse_simple_yaml(REGISTRY_PATH)
    service_count = len(registry.get("services", {}))
    print(f"[Rout Monitor] Loaded {service_count} services from registry")

    dashboard_html = load_dashboard(DASHBOARD_PATH)
    print(f"[Rout Monitor] Dashboard loaded ({len(dashboard_html)} bytes)")

    # Initialize API token
    api_token = _get_or_generate_token()
    print(f"[Rout Monitor] API token: {api_token[:16]}..." if len(api_token) > 16 else f"[Rout Monitor] API token: {api_token}")

    state = MonitorState(registry)

    # Initialize handler class variables
    StatusHandler.state = state
    StatusHandler.dashboard_html = dashboard_html
    StatusHandler.api_token = api_token
    StatusHandler.rate_limiter = RateLimiter(max_requests=30, window_seconds=60)

    print(f"[Rout Monitor] Running initial health checks...")
    state.run_all_checks()
    status = state.get_status()
    for sid, sdata in status.get("services", {}).items():
        s = sdata.get("status", "unknown")
        print(f"  {sdata.get('display_name', sid)}: {s}")

    streak = status.get("uptime_streak", {})
    print(f"[Rout Monitor] Uptime streak: {streak.get('human', 'N/A')}")

    skills = status.get("skill_agents", {})
    if skills:
        print(f"[Rout Monitor] Skill agents: {len(skills)} scanned")

    triggers = status.get("proactive_triggers", {})
    if triggers:
        print(f"[Rout Monitor] Proactive triggers: {len(triggers)} tracked")

    checker = Thread(target=check_loop, args=(state, StatusHandler.rate_limiter), daemon=True)
    checker.start()

    HTTPServer.allow_reuse_address = True
    server = HTTPServer((BIND_HOST, STATUS_PORT), StatusHandler)
    print(f"[Rout Monitor] Serving on http://{BIND_HOST}:{STATUS_PORT}")
    print(f"[Rout Monitor] Dashboard: http://{BIND_HOST}:{STATUS_PORT}/")
    print(f"[Rout Monitor] API:       http://{BIND_HOST}:{STATUS_PORT}/api/status")
    print(f"[Rout Monitor] Keys: [V] V.A.T.S. diagnostic mode  [S] Sound toggle")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Rout Monitor] Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
