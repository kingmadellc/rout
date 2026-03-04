"""
launchd plist generation and management for Rout setup.
"""

import subprocess
from pathlib import Path

from .ui import ok, warn


def setup_ollama_autostart():
    """Configure Ollama to start on boot."""
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.ollama.serve.plist"
    if plist_path.exists():
        ok("Ollama auto-start already configured")
        return

    import shutil
    ollama_path = shutil.which("ollama") or "/usr/local/bin/ollama"
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ollama.serve</string>
    <key>ProgramArguments</key>
    <array>
        <string>{ollama_path}</string>
        <string>serve</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/ollama.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/ollama.err</string>
</dict>
</plist>"""

    try:
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(plist_content)
        subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, timeout=10)
        ok("Ollama auto-start configured")
    except Exception:
        warn("Could not configure Ollama auto-start. Run manually: ollama serve")
