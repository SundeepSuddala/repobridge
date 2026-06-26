#!/usr/bin/env bash
# install.sh - one-shot setup for repobridge MCP server + Claude Code hooks
#
# What this does:
#   1. Creates .venv + installs repobridge (if not already set up)
#   2. Prompts for repo root directories -> writes ~/.repobridge.json
#      (if already configured, skips; if skipped, lazy-create hook handles it)
#   3. Registers the MCP server globally (user scope in Claude Code)
#   4. Installs two PreToolUse hooks into ~/.claude/settings.json:
#        - hooks/repobridge-nudge.py       (Bash -> ask when targeting other repos)
#        - hooks/repobridge-ensure-roots.py (mcp__repobridge__* -> prompt if no roots)
#
# Idempotent: safe to re-run. Backs up settings.json before touching it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON="$VENV_DIR/bin/python3"
SETTINGS="$HOME/.claude/settings.json"
NUDGE_HOOK="$SCRIPT_DIR/hooks/repobridge-nudge.py"
ROOTS_HOOK="$SCRIPT_DIR/hooks/repobridge-ensure-roots.py"
REPOBRIDGE_CFG="$HOME/.repobridge.json"

# ── Colours (no-op when not a TTY) ──────────────────────────────────────────
_tty() { [ -t 1 ]; }
bold()  { _tty && printf '\033[1m%s\033[0m\n' "$*" || echo "$*"; }
green() { _tty && printf '\033[0;32m%s\033[0m\n' "$*" || echo "$*"; }
blue()  { _tty && printf '\033[0;34m%s\033[0m\n' "$*" || echo "$*"; }
warn()  { _tty && printf '\033[0;33mWARN: %s\033[0m\n' "$*" >&2 || echo "WARN: $*" >&2; }

echo ""
bold "=== repobridge installer ==="
echo ""

# ── Step 1: Python venv + package install ────────────────────────────────────
blue "[1/4] Setting up Python environment..."
if [ ! -f "$PYTHON" ]; then
    echo "  Creating virtual environment at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi
echo "  Installing repobridge..."
"$PYTHON" -m pip install -e "$SCRIPT_DIR" --quiet

chmod +x "$NUDGE_HOOK" "$ROOTS_HOOK"
green "  Done."

# ── Step 2: Configure ~/.repobridge.json ─────────────────────────────────────
blue "[2/4] Configuring repo roots..."

_has_roots() {
    # Returns 0 (true) if roots already configured
    [ -f "$REPOBRIDGE_CFG" ] && \
    "$PYTHON" - "$REPOBRIDGE_CFG" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    roots = d.get("repo_roots", [])
    sys.exit(0 if roots and any(r.strip() for r in roots) else 1)
except Exception:
    sys.exit(1)
PYEOF
}

if _has_roots; then
    green "  Roots already configured in $REPOBRIDGE_CFG - skipping."
else
    echo ""
    echo "  repobridge needs to know which directories CONTAIN your Git repos."
    echo "  Each path should be a folder whose subdirectories are Git repos, e.g.:"
    echo "    ~/Documents/GitHub/Backend  (which contains user-service/, order-service/, ...)"
    echo ""
    if _tty; then
        printf "  Enter repo root dirs (colon-separated, e.g. ~/GitHub/Backend:~/GitHub/Own), or press Enter to skip:\n  > "
        read -r ROOT_INPUT || ROOT_INPUT=""
    else
        ROOT_INPUT=""
        warn "Non-interactive mode - skipping root prompt. Configure $REPOBRIDGE_CFG manually."
    fi

    if [ -n "$ROOT_INPUT" ]; then
        # Write roots to ~/.repobridge.json, preserving any existing keys
        "$PYTHON" - "$REPOBRIDGE_CFG" "$ROOT_INPUT" <<'PYEOF'
import json, os, sys
cfg_path = sys.argv[1]
raw_roots = [r.strip() for r in sys.argv[2].split(":") if r.strip()]
expanded = [os.path.expanduser(r) for r in raw_roots]

try:
    existing = json.load(open(cfg_path)) if os.path.exists(cfg_path) else {}
except (json.JSONDecodeError, OSError):
    existing = {}

existing["repo_roots"] = expanded
# Ensure sensible defaults if not already present
existing.setdefault("max_output", 20000)
existing.setdefault("cache_ttl_seconds", 300)
existing.setdefault("auth_token", "")

with open(cfg_path, "w") as f:
    json.dump(existing, f, indent=2)
    f.write("\n")
print(f"  Written {len(expanded)} root(s) to {cfg_path}")
PYEOF
        green "  Saved."
    else
        # Skipped - write template if no config exists
        if [ ! -f "$REPOBRIDGE_CFG" ]; then
            cp "$SCRIPT_DIR/example-config.json" "$REPOBRIDGE_CFG"
            warn "Skipped - copied example config to $REPOBRIDGE_CFG"
            echo "  Edit it to set repo_roots before using repobridge."
            echo "  Or: the ensure-roots hook will prompt Claude to configure it automatically."
        else
            warn "Skipped - $REPOBRIDGE_CFG exists but has no valid roots."
            echo "  The ensure-roots hook will prompt Claude to configure it on next use."
        fi
    fi
fi

# ── Step 3: Register MCP server globally ─────────────────────────────────────
blue "[3/4] Registering MCP server (global / user scope)..."

if command -v claude &>/dev/null; then
    # Check if already registered
    if claude mcp list 2>/dev/null | grep -q "repobridge"; then
        green "  repobridge already registered in Claude Code - skipping."
    else
        claude mcp add repobridge -s user -- "$PYTHON" "$SCRIPT_DIR/server.py"
        green "  Registered via 'claude mcp add'."
    fi
else
    # Fallback: merge directly into ~/.claude.json
    warn "'claude' CLI not found - falling back to direct ~/.claude.json edit."
    "$PYTHON" - "$PYTHON" "$SCRIPT_DIR/server.py" <<'PYEOF'
import json, os, sys
python_bin = sys.argv[1]
server_py  = sys.argv[2]
cfg_path   = os.path.expanduser("~/.claude.json")

try:
    cfg = json.load(open(cfg_path)) if os.path.exists(cfg_path) else {}
except (json.JSONDecodeError, OSError):
    cfg = {}

servers = cfg.setdefault("mcpServers", {})
if "repobridge" in servers:
    print("  repobridge already in ~/.claude.json - skipping.")
else:
    servers["repobridge"] = {"command": python_bin, "args": [server_py]}
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print(f"  Written repobridge entry to {cfg_path}")
PYEOF
fi

# ── Step 4: Install PreToolUse hooks ─────────────────────────────────────────
blue "[4/4] Installing Claude Code hooks into $SETTINGS..."

mkdir -p "$(dirname "$SETTINGS")"

# Backup before touching
if [ -f "$SETTINGS" ]; then
    cp "$SETTINGS" "${SETTINGS}.bak"
    echo "  Backed up to ${SETTINGS}.bak"
fi

"$PYTHON" - "$SETTINGS" "$NUDGE_HOOK" "$ROOTS_HOOK" <<'PYEOF'
import json, os, sys

settings_path = sys.argv[1]
nudge_cmd     = sys.argv[2]
roots_cmd     = sys.argv[3]

# Load or start fresh
try:
    settings = json.load(open(settings_path)) if os.path.exists(settings_path) else {}
except (json.JSONDecodeError, OSError):
    settings = {}

hooks_section = settings.setdefault("hooks", {})
pretool = hooks_section.setdefault("PreToolUse", [])

def already_registered(entries, cmd):
    """Return True if any hook entry's command matches cmd."""
    for entry in entries:
        for hook in entry.get("hooks", []):
            if hook.get("command") == cmd:
                return True
    return False

added = []

# 1 - Bash -> nudge hook (ask when cross-repo bash detected)
if not already_registered(pretool, nudge_cmd):
    pretool.append({
        "matcher": "Bash",
        "hooks": [
            {
                "type": "command",
                "command": nudge_cmd,
                "timeout": 5
            }
        ]
    })
    added.append("repobridge-nudge (Bash)")
else:
    print(f"  repobridge-nudge already registered - skipping.")

# 2 - mcp__repobridge__* -> ensure-roots hook
if not already_registered(pretool, roots_cmd):
    pretool.append({
        "matcher": "mcp__repobridge__",
        "hooks": [
            {
                "type": "command",
                "command": roots_cmd,
                "timeout": 5
            }
        ]
    })
    added.append("repobridge-ensure-roots (mcp__repobridge__*)")
else:
    print(f"  repobridge-ensure-roots already registered - skipping.")

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

if added:
    print(f"  Added hook(s): {', '.join(added)}")
PYEOF

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
bold "=== Installation complete ==="
echo ""
echo "  Hooks installed:  $SETTINGS"
echo "  Config:           $REPOBRIDGE_CFG"
echo "  MCP server:       $PYTHON $SCRIPT_DIR/server.py"
echo ""
green "Restart Claude Code to load the new hook and MCP server."
echo ""
echo "To remove hooks, delete the two 'repobridge' entries from:"
echo "  $SETTINGS"
echo ""
