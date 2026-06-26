#!/usr/bin/env python3
"""
repobridge PreToolUse hook - MCP tool interceptor for missing roots.

Matched against tool names matching 'mcp__repobridge__.*'. When
~/.repobridge.json has no repo_roots configured, surfaces a prompt asking
Claude to find out the user's repo root directories and write them to
~/.repobridge.json before retrying the repobridge tool.

This implements the "lazy-create" path: if roots were skipped during install,
the next repobridge tool call will prompt for them automatically.

Exit 0 always - a bug here must never block MCP tool calls.
"""
from __future__ import annotations

import os
import sys

# shared config module lives one level up (project root)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from repobridge_config import has_roots as _has_roots


def _ask_configure() -> None:
    """Emit hookSpecificOutput asking Claude to configure roots first."""
    reason = (
        "repobridge has no repo_roots configured. "
        "Ask the user which directories contain their Git repositories "
        "(each directory should hold multiple repos as subdirectories), "
        "then write them to ~/.repobridge.json as the 'repo_roots' array "
        "(e.g. [\"~/Documents/GitHub/Backend\", \"~/Documents/GitHub/Own\"]). "
        "After writing the file, retry the repobridge tool."
    )
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(output))


def main() -> None:
    try:
        # Consume stdin even if we don't need the payload
        sys.stdin.read()
    except OSError:
        pass

    try:
        if not _has_roots():
            _ask_configure()
    except Exception:  # noqa: BLE001 - hook must never crash
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
