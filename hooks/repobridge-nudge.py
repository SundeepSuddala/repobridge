#!/usr/bin/env python3
"""
repobridge PreToolUse hook - Bash tool interceptor.

When Claude runs a read/search bash command (grep, find, cat, head, tail, ls,
git -C, rg, ag, awk, sed) that targets a path inside a *different* repo than
the current working directory, this hook surfaces a permission prompt asking
Claude to use mcp__repobridge__* tools instead (faster, cached, token-cheaper).

Write commands, the current project, and commands aimed outside any known repo
are all let through silently.

Hook JSON contract (Claude Code PreToolUse):
  stdin: JSON payload with .tool_input.command and .cwd
  stdout: hookSpecificOutput JSON to surface a prompt, or empty to allow.
  exit 0 always - a bug here must never block the Bash tool.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

# shared config module lives one level up (project root)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from repobridge_config import load_roots, discover_repos

# Verbs that indicate a read/search-only intent.
_READ_VERBS = frozenset({
    "grep", "egrep", "fgrep", "rg", "ag",
    "find", "locate",
    "cat", "head", "tail", "less", "more",
    "ls", "dir", "tree",
    "sed", "awk",
    "wc", "stat", "file",
    "git",
})

# Build args of these tools that are themselves paths (skip the next token).
_PATH_FLAGS = frozenset({"-C", "--directory", "-f", "--file"})

# Pattern: looks like a filesystem path (absolute, home-relative, or ~user).
_PATH_RE = re.compile(r"^(/|~/|~[a-zA-Z])")


def _load_roots() -> list[Path]:
    return load_roots()


def _discover_repos(roots: list[Path]) -> dict[str, Path]:
    return discover_repos(roots)


def _current_repo(cwd: Path, repos: dict[str, Path]) -> str | None:
    """Return the repo name whose path is a parent of cwd, or None."""
    for name, repo_path in repos.items():
        try:
            cwd.relative_to(repo_path)
            return name
        except ValueError:
            pass
    return None


def _is_read_verb(verb: str) -> bool:
    return verb in _READ_VERBS


def _extract_path_tokens(command: str, cwd: Path) -> list[Path]:
    """
    Extract filesystem path tokens from a shell command string.
    Handles:
      - Absolute paths (/foo/bar)
      - Home-relative paths (~/foo, ~user/foo)
      - cd <path> targets (resolve relative to cwd)
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Fall back to whitespace split on unbalanced quotes
        tokens = command.split()

    paths: list[Path] = []
    skip_next = False
    first_verb = True
    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if skip_next:
            skip_next = False
            # This token is a value for a flag - could itself be a path
            if _PATH_RE.match(tok) or (not tok.startswith("-")):
                try:
                    p = Path(os.path.expanduser(tok)).resolve() if not tok.startswith("/") \
                        else Path(tok).resolve()
                    paths.append(p)
                except (ValueError, OSError):
                    pass
            i += 1
            continue

        # Gate: only proceed if first non-flag token is a read verb
        if first_verb and not tok.startswith("-"):
            first_verb = False
            verb = os.path.basename(tok)  # handles /usr/bin/grep etc.
            if not _is_read_verb(verb):
                return []  # Write/execute verb - let it through
            i += 1
            continue

        # git -C <path> pattern - capture the path after -C
        if tok == "-C" and i + 1 < len(tokens):
            next_tok = tokens[i + 1]
            try:
                p = Path(os.path.expanduser(next_tok)).resolve()
                paths.append(p)
            except (ValueError, OSError):
                pass
            i += 2
            continue

        # Flag that takes a path value
        if tok in _PATH_FLAGS:
            skip_next = True
            i += 1
            continue

        # Skip other flags
        if tok.startswith("-"):
            i += 1
            continue

        # cd <path> - treat destination as the path to evaluate
        if tok == "cd" and i + 1 < len(tokens):
            next_tok = tokens[i + 1]
            try:
                p = (cwd / next_tok).resolve() if not (
                    next_tok.startswith("/") or next_tok.startswith("~")
                ) else Path(os.path.expanduser(next_tok)).resolve()
                paths.append(p)
            except (ValueError, OSError):
                pass
            i += 2
            continue

        # Absolute or home-relative path tokens
        if _PATH_RE.match(tok):
            try:
                paths.append(Path(os.path.expanduser(tok)).resolve())
            except (ValueError, OSError):
                pass

        i += 1

    return paths


def _repo_for_path(path: Path, repos: dict[str, Path]) -> str | None:
    """Return the repo name that contains path, or None."""
    for name, repo_path in repos.items():
        try:
            path.relative_to(repo_path)
            return name
        except ValueError:
            pass
    return None


def _ask(repo_name: str) -> None:
    """Emit the hookSpecificOutput JSON asking Claude to use repobridge."""
    reason = (
        f"Path is in repo '{repo_name}'. "
        "Use mcp__repobridge__search_code / read_file / list_files / "
        "find_repos_with for cross-repo access - faster, cached, "
        "and token-cheaper than bash."
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
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError):
        sys.exit(0)

    try:
        command = payload.get("tool_input", {}).get("command", "")
        cwd_str = payload.get("cwd", "") or os.getcwd()
        cwd = Path(os.path.expanduser(cwd_str)).resolve()
    except (ValueError, OSError):
        sys.exit(0)

    try:
        roots = _load_roots()
        if not roots:
            sys.exit(0)

        repos = _discover_repos(roots)
        if not repos:
            sys.exit(0)

        current = _current_repo(cwd, repos)

        # Handle multi-command pipelines/chains - check each segment
        # Split on &&, ||, ;, | and evaluate each part independently
        segments = re.split(r"&&|\|\||;|\|", command)

        for segment in segments:
            segment = segment.strip()
            if not segment:
                continue

            path_tokens = _extract_path_tokens(segment, cwd)

            for path in path_tokens:
                target_repo = _repo_for_path(path, repos)
                if target_repo and target_repo != current:
                    _ask(target_repo)
                    sys.exit(0)

    except Exception:  # noqa: BLE001 - hook must never crash
        sys.exit(0)


if __name__ == "__main__":
    main()
