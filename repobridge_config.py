"""Shared config loading and repo discovery for server and hooks."""
from __future__ import annotations

import json
import os
from pathlib import Path


def load_roots() -> list[Path]:
    """Load repo roots: REPOBRIDGE_ROOTS env var > ~/.repobridge.json > empty list."""
    env_roots = os.environ.get("REPOBRIDGE_ROOTS", "")
    if env_roots:
        return [
            Path(os.path.expanduser(r.strip())).resolve()
            for r in env_roots.split(":") if r.strip()
        ]
    cfg_path = Path.home() / ".repobridge.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            raw = cfg.get("repo_roots")
            if raw and isinstance(raw, list):
                return [Path(os.path.expanduser(r)).resolve() for r in raw if r]
        except (json.JSONDecodeError, OSError):
            pass
    return []


def has_roots() -> bool:
    """Return True if any repo roots are configured."""
    env_roots = os.environ.get("REPOBRIDGE_ROOTS", "")
    if env_roots and any(r.strip() for r in env_roots.split(":")):
        return True
    cfg_path = Path.home() / ".repobridge.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            raw = cfg.get("repo_roots")
            if raw and isinstance(raw, list) and any(r for r in raw if r):
                return True
        except (json.JSONDecodeError, OSError):
            pass
    return False


def discover_repos(roots: list[Path]) -> dict[str, Path]:
    """Scan roots one level deep for .git directories. Returns {name: path}."""
    repos: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for entry in sorted(root.iterdir()):
                if entry.is_dir() and (entry / ".git").exists():
                    repos[entry.name] = entry.resolve()
        except PermissionError:
            pass
    return repos
