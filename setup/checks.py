"""
Dependency checks and environment validation for Rout setup.
"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from .ui import ok, warn, fail, BOLD, NC


def get_system_ram_gb():
    """Get total system RAM in GB."""
    try:
        if platform.system() == "Darwin":
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5,
            )
            return int(result.stdout.strip()) // (1024 ** 3)
        else:
            # Linux fallback
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal"):
                        kb = int(line.split()[1])
                        return kb // (1024 * 1024)
    except Exception:
        pass
    return 0


def is_apple_silicon():
    """Check if running on Apple Silicon."""
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def check_core_dependencies():
    """
    Check dependencies needed by both cloud and local modes.

    Returns:
        (issues, warnings) — lists of strings
    """
    issues = []
    warnings = []

    print(f"  {BOLD}Core dependencies:{NC}")

    # Python version
    py_version = sys.version_info
    if py_version >= (3, 9):
        ok(f"Python {py_version.major}.{py_version.minor}.{py_version.micro}")
    else:
        issues.append(
            f"Python 3.9+ required, found {py_version.major}.{py_version.minor}.{py_version.micro}\n"
            f"  Install via Homebrew: brew install python@3.11"
        )

    # Python packages
    for pkg, import_name in [("pyyaml", "yaml"), ("requests", "requests"), ("python-dateutil", "dateutil")]:
        try:
            __import__(import_name)
        except ImportError:
            print(f"  Installing {pkg}...")
            installed = False
            for cmd in [
                [sys.executable, "-m", "pip", "install", pkg, "--quiet", "--user"],
                [sys.executable, "-m", "pip", "install", pkg, "--quiet", "--break-system-packages"],
            ]:
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    installed = True
                    break
            try:
                __import__(import_name)
                ok(f"{pkg} installed")
            except ImportError:
                if installed:
                    issues.append(f"{pkg} installed but still not importable in this Python ({sys.executable})")
                else:
                    issues.append(
                        f"Failed to install {pkg}. Try manually:\n"
                        f"  {sys.executable} -m pip install --user {pkg}"
                    )

    # Homebrew
    brew_path = shutil.which("brew")
    if brew_path:
        ok(f"Homebrew found at {brew_path}")
    else:
        issues.append(
            "Homebrew not found — install from: https://brew.sh\n"
            '  Run: /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
        )

    # imsg CLI
    imsg_path = shutil.which("imsg") or "/opt/homebrew/bin/imsg"
    if os.path.isfile(imsg_path) and os.access(imsg_path, os.X_OK):
        ok(f"imsg CLI found at {imsg_path}")
    else:
        issues.append("imsg CLI not found — install with: brew install imsg")

    # macOS version check
    try:
        result = subprocess.run(["sw_vers", "-productVersion"], capture_output=True, text=True, timeout=5)
        macos_version = result.stdout.strip()
        major = int(macos_version.split(".")[0])
        if major >= 12:
            ok(f"macOS {macos_version}")
        else:
            warnings.append(f"macOS 12+ recommended, found {macos_version}")
    except Exception:
        pass

    return issues, warnings


def check_cloud_dependencies():
    """
    Check additional dependencies needed for cloud mode.

    Returns:
        issues — list of strings
    """
    issues = []

    print(f"\n  {BOLD}Cloud mode dependencies:{NC}")

    # Node.js (needed for OpenClaw)
    node_path = shutil.which("node")
    if node_path:
        try:
            result = subprocess.run([node_path, "--version"], capture_output=True, text=True, timeout=5)
            ok(f"Node.js {result.stdout.strip()}")
        except Exception:
            ok(f"Node.js found at {node_path}")
    else:
        issues.append("Node.js not found — install with: brew install node")

    # OpenClaw
    openclaw_path = shutil.which("openclaw")
    if openclaw_path:
        try:
            result = subprocess.run([openclaw_path, "--version"], capture_output=True, text=True, timeout=5)
            version = result.stdout.strip() or "installed"
            ok(f"OpenClaw {version}")
        except Exception:
            ok(f"OpenClaw found at {openclaw_path}")
    else:
        issues.append(
            "OpenClaw not found — install with: npm install -g openclaw\n"
            "  See: https://openclaw.ai"
        )

    return issues


def check_local_dependencies():
    """
    Check dependencies needed for local mode.

    Returns:
        (issues, ollama_installed) — (list of strings, bool)
    """
    issues = []
    ollama_installed = False

    print(f"\n  {BOLD}Local mode dependencies:{NC}")

    # Apple Silicon check
    if is_apple_silicon():
        ok("Apple Silicon detected")
    elif platform.system() == "Darwin":
        warn("Intel Mac detected — local models will be slow. Apple Silicon recommended.")
    else:
        warn("Non-macOS system — Ollama should still work but is untested with Rout.")

    # RAM check
    ram_gb = get_system_ram_gb()
    if ram_gb >= 16:
        ok(f"{ram_gb}GB RAM detected")
    elif ram_gb > 0:
        issues.append(f"Only {ram_gb}GB RAM detected. Minimum 16GB for local models.")
    else:
        warn("Could not detect RAM. Ensure at least 16GB for local models.")

    # Ollama
    ollama_path = shutil.which("ollama")
    if ollama_path:
        try:
            result = subprocess.run([ollama_path, "--version"], capture_output=True, text=True, timeout=5)
            ok(f"Ollama {result.stdout.strip()}")
        except Exception:
            ok(f"Ollama found at {ollama_path}")
        ollama_installed = True
    else:
        warn("Ollama not installed — will install during setup")

    return issues, ollama_installed
