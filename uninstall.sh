#!/usr/bin/env bash
# uninstall.sh - remove repobridge hooks, MCP registration, and optionally config
#
# Reverses install.sh:
#   1. Removes the two PreToolUse hook entries from ~/.claude/settings.json
#   2. Unregisters the MCP server (user scope)
#   3. Optionally removes ~/.repobridge.json and .venv (asked interactively)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"
SETTINGS="$HOME/.claude/settings.json"
NUDGE_HOOK="$SCRIPT_DIR/hooks/repobridge-nudge.py"
ROOTS_HOOK="$SCRIPT_DIR/hooks/repobridge-ensure-roots.py"
REPOBRIDGE_CFG="$HOME/.repobridge.json"

_tty() { [ -t 1 ]; }
bold()  { _tty && printf '\033[1m%s\033[0m\n' "$*" || echo "$*"; }
green() { _tty && printf '\033[0;32m%s\033[0m\n' "$*" || echo "$*"; }
blue()  { _tty && printf '\033[0;34m%s\033[0m\n' "$*" || echo "$*"; }
warn()  { _tty && printf '\033[0;33mWARN: %s\033[0m\n' "$*" >&2 || echo "WARN: $*" >&2; }

echo ""
bold "=== repobridge uninstaller ==="
echo ""

# Resolve python - fall back to system python3 if venv gone
if [ ! -f "$PYTHON" ]; then
    PYTHON="$(command -v python3 || true)"
    if [ -z "$PYTHON" ]; then
        echo "ERROR: python3 not found - cannot patch JSON files." >&2
        exit 1
    fi
fi

# ── Step 1: Remove hooks from ~/.claude/settings.json ────────────────────────
blue "[1/3] Removing hooks from $SETTINGS..."

if [ ! -f "$SETTINGS" ]; then
    warn "  $SETTINGS not found - nothing to remove."
else
    cp "$SETTINGS" "${SETTINGS}.bak"
    echo "  Backed up to ${SETTINGS}.bak"

    "$PYTHON" - "$SETTINGS" "$NUDGE_HOOK" "$ROOTS_HOOK" <<'PYEOF'
import json, sys

settings_path = sys.argv[1]
nudge_cmd     = sys.argv[2]
roots_cmd     = sys.argv[3]

try:
    settings = json.load(open(settings_path))
except (json.JSONDecodeError, OSError) as e:
    print(f"  Could not read {settings_path}: {e}")
    sys.exit(0)

def remove_hook_entries(entries, cmd):
    """Remove any hook group that contains a hook with the given command."""
    before = len(entries)
    filtered = [
        entry for entry in entries
        if not any(h.get("command") == cmd for h in entry.get("hooks", []))
    ]
    return filtered, before - len(filtered)

removed = 0
pretool = settings.get("hooks", {}).get("PreToolUse", [])

pretool, n = remove_hook_entries(pretool, nudge_cmd)
removed += n

pretool, n = remove_hook_entries(pretool, roots_cmd)
removed += n

if removed:
    settings.setdefault("hooks", {})["PreToolUse"] = pretool
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    print(f"  Removed {removed} hook entry/entries.")
else:
    print("  No repobridge hooks found in settings - nothing removed.")
PYEOF
fi

# ── Step 2: Unregister MCP server ────────────────────────────────────────────
blue "[2/3] Unregistering MCP server..."

if command -v claude &>/dev/null; then
    if claude mcp list 2>/dev/null | grep -q "repobridge"; then
        claude mcp remove repobridge -s user
        green "  Unregistered via 'claude mcp remove'."
    else
        echo "  repobridge not found in Claude Code MCP list - skipping."
    fi
else
    warn "  'claude' CLI not found - attempting direct ~/.claude.json edit."
    "$PYTHON" - <<'PYEOF'
import json, os, sys
cfg_path = os.path.expanduser("~/.claude.json")
if not os.path.exists(cfg_path):
    print("  ~/.claude.json not found - skipping.")
    sys.exit(0)
try:
    cfg = json.load(open(cfg_path))
except (json.JSONDecodeError, OSError) as e:
    print(f"  Could not read {cfg_path}: {e}")
    sys.exit(0)
servers = cfg.get("mcpServers", {})
if "repobridge" in servers:
    del servers["repobridge"]
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print("  Removed repobridge from ~/.claude.json.")
else:
    print("  repobridge not found in ~/.claude.json - skipping.")
PYEOF
fi

# ── Step 3: Optional cleanup ─────────────────────────────────────────────────
blue "[3/3] Optional cleanup..."

_confirm() {
    local prompt="$1"
    if _tty; then
        printf "  %s [y/N] " "$prompt"
        read -r answer </dev/tty || answer="n"
        [[ "$answer" =~ ^[Yy]$ ]]
    else
        return 1  # non-interactive: skip optional removals
    fi
}

if [ -f "$REPOBRIDGE_CFG" ]; then
    if _confirm "Remove $REPOBRIDGE_CFG?"; then
        rm "$REPOBRIDGE_CFG"
        green "  Removed $REPOBRIDGE_CFG."
    else
        echo "  Kept $REPOBRIDGE_CFG."
    fi
fi

if [ -d "$SCRIPT_DIR/.venv" ]; then
    if _confirm "Remove .venv (Python virtualenv)?"; then
        rm -rf "$SCRIPT_DIR/.venv"
        green "  Removed .venv."
    else
        echo "  Kept .venv."
    fi
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
bold "=== Uninstall complete ==="
echo ""
green "Restart Claude Code to fully unload the hook and MCP server."
echo ""
