#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# Rout Local-Only Setup — Qwen 3.5 via Ollama on Mac Mini (32GB)
# ──────────────────────────────────────────────────────────────────────────────
#
# What this does:
#   1. Installs Ollama (if not present)
#   2. Pulls Qwen 3.5 27B (Q4 quantized — fits in 32GB with headroom)
#   3. Configures macOS GPU memory cap for optimal performance
#   4. Generates local-only config.yaml
#   5. Validates everything works with a test inference
#
# Run: bash setup_local.sh
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; }
info() { echo -e "${CYAN}[→]${NC} $1"; }

# ── System Check ─────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Rout Local-Only Setup — Qwen 3.5 on Apple Silicon"
echo "═══════════════════════════════════════════════════════════"
echo ""

# Check macOS
if [[ "$(uname)" != "Darwin" ]]; then
    err "This script is for macOS only."
    exit 1
fi

# Check Apple Silicon
ARCH=$(uname -m)
if [[ "$ARCH" != "arm64" ]]; then
    err "Apple Silicon (M1/M2/M3/M4) required. Detected: $ARCH"
    exit 1
fi

# Check RAM
RAM_GB=$(sysctl -n hw.memsize | awk '{printf "%.0f", $1/1073741824}')
info "Detected: Apple Silicon ($ARCH) with ${RAM_GB}GB RAM"

if [[ "$RAM_GB" -lt 16 ]]; then
    err "Minimum 16GB RAM required. You have ${RAM_GB}GB."
    exit 1
fi

# Model selection based on RAM
MODEL="qwen3.5:27b"
MODEL_DISPLAY="Qwen 3.5 27B (Q4)"
GPU_MEM_MB=24576

if [[ "$RAM_GB" -ge 48 ]]; then
    # 48GB+ can comfortably run the 35B MoE
    MODEL="qwen3.5:35b-a3b"
    MODEL_DISPLAY="Qwen 3.5 35B-A3B (MoE, Q4)"
    GPU_MEM_MB=32768
    log "RAM: ${RAM_GB}GB — using $MODEL_DISPLAY (MoE, larger model)"
elif [[ "$RAM_GB" -ge 32 ]]; then
    log "RAM: ${RAM_GB}GB — using $MODEL_DISPLAY (optimal for your hardware)"
elif [[ "$RAM_GB" -ge 16 ]]; then
    MODEL="qwen3.5:14b"
    MODEL_DISPLAY="Qwen 3.5 14B (Q4)"
    GPU_MEM_MB=12288
    warn "RAM: ${RAM_GB}GB — using $MODEL_DISPLAY (smaller model for limited RAM)"
fi

echo ""

# ── Install Ollama ───────────────────────────────────────────────────────────

if command -v ollama &>/dev/null; then
    OLLAMA_VERSION=$(ollama --version 2>/dev/null || echo "unknown")
    log "Ollama already installed: $OLLAMA_VERSION"
else
    info "Installing Ollama..."
    if command -v brew &>/dev/null; then
        brew install ollama
    else
        curl -fsSL https://ollama.com/install.sh | sh
    fi
    log "Ollama installed"
fi

# ── Start Ollama (if not running) ────────────────────────────────────────────

if ! curl -s http://localhost:11434/api/tags &>/dev/null; then
    info "Starting Ollama server..."
    ollama serve &>/dev/null &
    OLLAMA_PID=$!
    sleep 3

    if ! curl -s http://localhost:11434/api/tags &>/dev/null; then
        err "Ollama failed to start. Check: ollama serve"
        exit 1
    fi
    log "Ollama server started (PID: $OLLAMA_PID)"
else
    log "Ollama server already running"
fi

# ── GPU Memory Cap (critical for 32GB machines) ─────────────────────────────

info "Setting GPU memory cap to ${GPU_MEM_MB}MB..."
sudo sysctl iogpu.wired_limit_mb=$GPU_MEM_MB 2>/dev/null || {
    warn "Could not set GPU memory cap (may need sudo). Continuing..."
}
log "GPU memory cap configured"

# ── Pull Model ───────────────────────────────────────────────────────────────

info "Pulling $MODEL_DISPLAY — this may take 10-30 minutes on first run..."
echo ""
ollama pull "$MODEL"
echo ""
log "Model $MODEL_DISPLAY downloaded"

# ── Test Inference ───────────────────────────────────────────────────────────

info "Running test inference..."
TEST_RESULT=$(curl -s http://localhost:11434/api/chat \
    -d "{
        \"model\": \"$MODEL\",
        \"messages\": [{\"role\": \"user\", \"content\": \"Say 'Rout is online' in exactly 3 words.\"}],
        \"stream\": false,
        \"options\": {\"num_predict\": 20}
    }" 2>/dev/null)

if echo "$TEST_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['message']['content'])" 2>/dev/null; then
    log "Test inference successful"
else
    err "Test inference failed. Check Ollama logs: ollama logs"
    echo "Raw response: $TEST_RESULT"
    exit 1
fi

# ── Test Tool Calling ────────────────────────────────────────────────────────

info "Testing tool calling capability..."
TOOL_TEST=$(curl -s http://localhost:11434/api/chat \
    -d "{
        \"model\": \"$MODEL\",
        \"messages\": [{\"role\": \"user\", \"content\": \"Search the web for today's weather.\"}],
        \"tools\": [{
            \"type\": \"function\",
            \"function\": {
                \"name\": \"web_search\",
                \"description\": \"Search the web for current information.\",
                \"parameters\": {
                    \"type\": \"object\",
                    \"properties\": {
                        \"query\": {\"type\": \"string\", \"description\": \"Search query\"}
                    },
                    \"required\": [\"query\"]
                }
            }
        }],
        \"stream\": false,
        \"options\": {\"num_predict\": 100}
    }" 2>/dev/null)

HAS_TOOL_CALLS=$(echo "$TOOL_TEST" | python3 -c "
import sys, json
d = json.load(sys.stdin)
tc = d.get('message', {}).get('tool_calls', [])
if tc:
    print(f'Tool call: {tc[0][\"function\"][\"name\"]}({json.dumps(tc[0][\"function\"][\"arguments\"])})')
else:
    print('NO_TOOL_CALLS')
" 2>/dev/null)

if [[ "$HAS_TOOL_CALLS" == "NO_TOOL_CALLS" ]]; then
    warn "Model did not use tool calling on test prompt. This may work in practice with better prompts."
else
    log "Tool calling works: $HAS_TOOL_CALLS"
fi

# ── Generate Config ──────────────────────────────────────────────────────────

CONFIG_DIR="$HOME/.openclaw"
CONFIG_PATH="$CONFIG_DIR/config.yaml"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
info "Generating local-only config..."

if [[ -f "$CONFIG_PATH" ]]; then
    BACKUP_PATH="${CONFIG_PATH}.backup.$(date +%s)"
    cp "$CONFIG_PATH" "$BACKUP_PATH"
    warn "Existing config backed up to: $BACKUP_PATH"
fi

# Read existing config values we want to preserve
EXISTING_NAME=""
EXISTING_LOCATION=""
EXISTING_TZ=""
EXISTING_LAT=""
EXISTING_LON=""
EXISTING_PERSONAL_ID=""
if [[ -f "$CONFIG_PATH" ]]; then
    EXISTING_NAME=$(python3 -c "import yaml; d=yaml.safe_load(open('$CONFIG_PATH')); print(d.get('user',{}).get('name',''))" 2>/dev/null || echo "")
    EXISTING_LOCATION=$(python3 -c "import yaml; d=yaml.safe_load(open('$CONFIG_PATH')); print(d.get('user',{}).get('location',''))" 2>/dev/null || echo "")
    EXISTING_TZ=$(python3 -c "import yaml; d=yaml.safe_load(open('$CONFIG_PATH')); print(d.get('user',{}).get('timezone',''))" 2>/dev/null || echo "")
    EXISTING_LAT=$(python3 -c "import yaml; d=yaml.safe_load(open('$CONFIG_PATH')); print(d.get('user',{}).get('latitude',''))" 2>/dev/null || echo "")
    EXISTING_LON=$(python3 -c "import yaml; d=yaml.safe_load(open('$CONFIG_PATH')); print(d.get('user',{}).get('longitude',''))" 2>/dev/null || echo "")
    EXISTING_PERSONAL_ID=$(python3 -c "import yaml; d=yaml.safe_load(open('$CONFIG_PATH')); print(d.get('chats',{}).get('personal_id',1))" 2>/dev/null || echo "1")
fi

# Merge: keep user settings, switch to local-only
python3 -c "
import yaml
from pathlib import Path

config_path = Path('$CONFIG_PATH')
existing = {}
if config_path.exists():
    with open(config_path) as f:
        existing = yaml.safe_load(f) or {}

# Preserve existing sections
user = existing.get('user', {})
chats = existing.get('chats', {'personal_id': 1, 'group_ids': []})
chat_handles = existing.get('chat_handles', {})
known_senders = existing.get('known_senders', {})
paths = existing.get('paths', {})
bluebubbles = existing.get('bluebubbles', {})
watcher = existing.get('watcher', {'history_limit': 10})
coinbase = existing.get('coinbase', {})
kalshi = existing.get('kalshi', {})

# Set defaults if empty
if not user.get('name'):
    user['name'] = '${EXISTING_NAME:-Your Name}'
if not user.get('assistant_name'):
    user['assistant_name'] = 'Rout'

# Build local-only config
config = {
    'user': user,
    'chats': chats,
    'chat_handles': chat_handles,
    'known_senders': known_senders,

    # LOCAL-ONLY MODE
    'local_only': True,

    # No cloud API keys needed
    # 'anthropic_api_key': removed — not needed in local mode

    'local_model': {
        'enabled': True,
        'provider': 'ollama',
        'host': 'http://localhost:11434',
        'model': '$MODEL',
        'timeout_seconds': 120,
        'max_tokens': 4096,
        'temperature': 0.7,
        'context_length': 32768,
        'gpu_memory_cap_mb': $GPU_MEM_MB,
    },

    # Disable cloud providers
    'codex': {'enabled': False},
    'anthropic': {'max_tokens': 4096},

    'paths': paths,
    'bluebubbles': bluebubbles,
    'watcher': watcher,
}

# Preserve optional integrations
if coinbase:
    config['coinbase'] = coinbase
if kalshi:
    config['kalshi'] = kalshi

config_path.parent.mkdir(parents=True, exist_ok=True)
with open(config_path, 'w') as f:
    f.write('# Rout Config — LOCAL-ONLY MODE (Qwen 3.5 via Ollama)\n')
    f.write('# No cloud API keys required. All inference runs on-device.\n')
    f.write('# Generated by: bash setup_local.sh\n')
    f.write('#\n')
    yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

print('Config written to: $CONFIG_PATH')
"

chmod 600 "$CONFIG_PATH"
log "Config generated with local-only mode enabled"

# ── Setup Ollama as LaunchDaemon (auto-start) ────────────────────────────────

info "Setting up Ollama auto-start..."
PLIST_PATH="$HOME/Library/LaunchAgents/com.ollama.serve.plist"

if [[ ! -f "$PLIST_PATH" ]]; then
    cat > "$PLIST_PATH" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ollama.serve</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/ollama</string>
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
</plist>
PLIST
    # Fix path if ollama is in homebrew
    OLLAMA_PATH=$(which ollama)
    sed -i '' "s|/usr/local/bin/ollama|$OLLAMA_PATH|g" "$PLIST_PATH"
    launchctl load "$PLIST_PATH" 2>/dev/null || true
    log "Ollama auto-start configured"
else
    log "Ollama auto-start already configured"
fi

# ── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Setup Complete"
echo "═══════════════════════════════════════════════════════════"
echo ""
log "Mode:        LOCAL-ONLY (zero cloud dependency)"
log "Model:       $MODEL_DISPLAY"
log "Host:        http://localhost:11434"
log "GPU Memory:  ${GPU_MEM_MB}MB allocated"
log "Config:      $CONFIG_PATH"
echo ""
info "To restart Rout with local-only mode:"
echo "  launchctl kickstart -k gui/\$(id -u)/com.rout.imsg-watcher"
echo ""
info "To monitor local inference:"
echo "  tail -f ~/.openclaw/workspace/token_usage.jsonl | python3 -m json.tool"
echo ""
info "To switch back to cloud mode:"
echo "  Edit $CONFIG_PATH → set local_only: false"
echo ""
echo "═══════════════════════════════════════════════════════════"
