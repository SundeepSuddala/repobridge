"""
repobridge MCP server — lets any AI agent navigate and work across all your
local Git repositories via the Model Context Protocol.

Compatible with: Claude Code, Claude Desktop, GitHub Copilot, Gemini,
Cursor, Windsurf, and any MCP-compatible client.

Transports:
  stdio             — default, used by most local MCP clients
  sse               — HTTP + Server-Sent Events (for remote/web clients)
  streamable-http   — newer HTTP streaming transport

Configuration (checked in order):
  1. Config file: ~/.repobridge.json
  2. Environment variables: REPOBRIDGE_ROOTS, REPOBRIDGE_MAX_OUTPUT, MCP_AUTH_TOKEN
  3. Built-in defaults

Tools exposed:
  list_repos          — list all repos with their git branch/status summary
  search_code         — grep for a pattern across one or all repos
  read_file           — read a file from a specific repo (with offset/limit)
  list_files          — list files matching a glob in a repo
  git_status          — git status for a repo
  git_diff            — git diff for a repo (unstaged, staged, or vs branch)
  git_log             — recent commits for a repo
  get_dependencies    — read build.gradle / pom.xml / package.json for a repo
  run_build           — run ./gradlew build (or mvn/npm) in a repo
  find_repos_with     — find which repos contain a class/method/config key
"""

from __future__ import annotations

import argparse
import hmac
import logging
import os
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("repobridge")

# ── Configuration ─────────────────────────────────────────────────────────────

_DEFAULT_REPO_ROOTS: list[str] = []  # Users must configure their own roots
_DEFAULT_MAX_OUTPUT = 20_000
_DEFAULT_CACHE_TTL = 300  # seconds
_ALLOWED_BUILD_TASKS = frozenset({
    "build", "test", "clean", "assemble", "check", "bootRun",
    "compileJava", "compileKotlin", "jar", "bootJar",
    "install", "package", "verify", "compile", "site",
    "start", "dev", "lint", "format", "typecheck",
})
_SAFE_EXTRA_ARG_RE = re.compile(r"^[\w.=:@-]+$")
_BLOCKED_ARG_PREFIXES = frozenset({
    "--init-script", "-I",
    "--project-dir", "-p",
    "--settings-file", "-c",
    "--build-file", "-b",
})


def _load_config() -> dict:
    """Load configuration from ~/.repobridge.json if it exists."""
    config_path = Path.home() / ".repobridge.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            logger.info("Loaded config from %s", config_path)
            return config
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load config from %s: %s", config_path, e)
            return {}
    return {}


_config = _load_config()


def _get_repo_roots() -> list[str]:
    """Resolve REPO_ROOTS from env var > config file > defaults."""
    env_roots = os.environ.get("REPOBRIDGE_ROOTS")
    if env_roots:
        roots = [os.path.expanduser(r.strip()) for r in env_roots.split(":") if r.strip()]
        logger.info("Using repo roots from REPOBRIDGE_ROOTS env var: %d directories", len(roots))
        return roots

    config_roots = _config.get("repo_roots")
    if config_roots and isinstance(config_roots, list):
        roots = [os.path.expanduser(r) for r in config_roots]
        logger.info("Using repo roots from config file: %d directories", len(roots))
        return roots

    if _DEFAULT_REPO_ROOTS:
        return _DEFAULT_REPO_ROOTS

    logger.warning(
        "No repo roots configured. Set REPOBRIDGE_ROOTS env var or create ~/.repobridge.json. "
        "See README.md for details."
    )
    return []


def _get_max_output() -> int:
    """Resolve MAX_OUTPUT from env var > config file > default."""
    env_val = os.environ.get("REPOBRIDGE_MAX_OUTPUT")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            logger.warning("Invalid REPOBRIDGE_MAX_OUTPUT value: %s, using default", env_val)

    config_val = _config.get("max_output")
    if isinstance(config_val, int) and config_val > 0:
        return config_val

    return _DEFAULT_MAX_OUTPUT


def _get_cache_ttl() -> int:
    """Resolve cache TTL from config file > default."""
    config_val = _config.get("cache_ttl_seconds")
    if isinstance(config_val, int) and config_val >= 0:
        return config_val
    return _DEFAULT_CACHE_TTL


REPO_ROOTS = _get_repo_roots()
MAX_OUTPUT = _get_max_output()
CACHE_TTL = _get_cache_ttl()
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", _config.get("auth_token", ""))

# Transport settings (configurable via CLI args, env vars, or config file)
_DEFAULT_TRANSPORT = "stdio"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7200

# ── Search backend detection ─────────────────────────────────────────────────

_HAS_RIPGREP = shutil.which("rg") is not None
if _HAS_RIPGREP:
    logger.info("Using ripgrep for search")
else:
    logger.info("ripgrep not found, falling back to grep")

# ── Caching ──────────────────────────────────────────────────────────────────

_repo_cache: dict[str, Path] | None = None
_repo_cache_time: float = 0.0
_repo_cache_lock = threading.Lock()


def _invalidate_cache() -> None:
    global _repo_cache, _repo_cache_time
    with _repo_cache_lock:
        _repo_cache = None
        _repo_cache_time = 0.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _all_repos() -> dict[str, Path]:
    """Return {repo_name: repo_path} for all git repos under REPO_ROOTS.

    Results are cached for CACHE_TTL seconds.
    """
    global _repo_cache, _repo_cache_time

    now = time.monotonic()
    with _repo_cache_lock:
        if _repo_cache is not None and (now - _repo_cache_time) < CACHE_TTL:
            return _repo_cache

        repos: dict[str, Path] = {}
        for root in REPO_ROOTS:
            root_path = Path(root)
            if not root_path.exists():
                logger.debug("Repo root does not exist, skipping: %s", root)
                continue
            for entry in sorted(root_path.iterdir()):
                if entry.is_dir() and (entry / ".git").exists():
                    repos[entry.name] = entry

        logger.debug("Discovered %d repositories", len(repos))
        _repo_cache = repos
        _repo_cache_time = now
        return repos


def _resolve_repo(repo_name: str) -> Path:
    """Resolve a repo name to its path, raising ValueError if not found."""
    if not repo_name or not repo_name.strip():
        raise ValueError("repo_name is required")

    repos = _all_repos()
    if repo_name not in repos:
        available = ", ".join(sorted(repos.keys()))
        raise ValueError(f"Repo '{repo_name}' not found. Available: {available}")
    return repos[repo_name]


def _validate_file_path(repo: Path, file_path: str) -> Path:
    """Resolve and validate a file path within a repo, preventing path traversal."""
    if not file_path or not file_path.strip():
        raise ValueError("file_path is required")

    repo_resolved = repo.resolve()
    try:
        full_path = (repo_resolved / file_path).resolve()
    except OSError:
        raise ValueError(f"Cannot resolve path: '{file_path}'")

    if not full_path.is_relative_to(repo_resolved):
        raise ValueError(f"Path traversal denied: '{file_path}' escapes repo boundary")

    return full_path


def _check_auth(token: str) -> str | None:
    """Validate auth token if MCP_AUTH_TOKEN is configured. Returns error string or None."""
    if not AUTH_TOKEN:
        return None
    if not hmac.compare_digest(token, AUTH_TOKEN):
        return "[AUTH ERROR] Invalid or missing auth token."
    return None


def _run(cmd: list[str], cwd: Path, timeout: int = 60) -> str:
    """Run a subprocess command and return its combined output."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        return output[:MAX_OUTPUT] if len(output) > MAX_OUTPUT else output
    except subprocess.TimeoutExpired:
        logger.warning("Command timed out after %ds: %s", timeout, " ".join(cmd))
        return f"[TIMEOUT after {timeout}s]"
    except FileNotFoundError:
        logger.error("Command not found: %s", cmd[0])
        return f"[ERROR] Command not found: {cmd[0]}"
    except OSError as e:
        logger.error("OS error running command %s: %s", cmd[0], e)
        return f"[ERROR] {e}"


def _truncate(text: str, label: str = "") -> str:
    """Truncate text to MAX_OUTPUT characters with an indicator."""
    if len(text) > MAX_OUTPUT:
        return text[:MAX_OUTPUT] + f"\n\n... [{label}truncated at {MAX_OUTPUT} chars]"
    return text


def _validate_extra_args(extra_args: str) -> list[str]:
    """Validate and split extra build arguments, preventing command injection."""
    if not extra_args:
        return []

    parts = extra_args.split()
    for part in parts:
        flag = part.split("=")[0]
        if flag in _BLOCKED_ARG_PREFIXES:
            raise ValueError(f"Build argument '{flag}' is not permitted.")
        if not _SAFE_EXTRA_ARG_RE.match(part):
            raise ValueError(
                f"Invalid build argument: '{part}'. "
                "Only alphanumeric, dots, equals, colons, at-signs, and hyphens are allowed."
            )
    return parts


def _build_search_cmd(
    pattern: str,
    file_glob: str,
    case_sensitive: bool,
    context_lines: int,
    files_only: bool = False,
) -> list[str]:
    """Build a search command using ripgrep (preferred) or grep (fallback)."""
    if _HAS_RIPGREP:
        cmd = ["rg"]
        if files_only:
            cmd.append("-l")
        else:
            cmd.extend(["-n", f"-C{context_lines}"])
        cmd.extend(["-g", file_glob])
        if not case_sensitive:
            cmd.append("-i")
        cmd.append(pattern)
    else:
        cmd = ["grep", "-rn" if not files_only else "-rl", f"--include={file_glob}"]
        if not files_only:
            cmd.append(f"-C{context_lines}")
        if not case_sensitive:
            cmd.append("-i")
        cmd.append(pattern)
    return cmd


# ── MCP Server ────────────────────────────────────────────────────────────────

mcp = FastMCP("repobridge")


@mcp.tool()
def list_repos(filter: str = "", auth: str = "") -> str:
    """
    List all available repositories with their current branch and dirty status.

    Args:
        filter: Optional substring to filter repo names (e.g. 'pricing', 'service')
        auth:   Auth token (required only if MCP_AUTH_TOKEN is set)
    """
    auth_err = _check_auth(auth)
    if auth_err:
        return auth_err

    repos = _all_repos()
    if not repos:
        return (
            "No repositories found. Configure repo_roots in ~/.repobridge.json "
            "or set REPOBRIDGE_ROOTS environment variable."
        )

    lines = []
    for name, path in repos.items():
        if filter and filter.lower() not in name.lower():
            continue
        branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], path).strip()
        dirty = _run(["git", "status", "--short"], path).strip()
        status = " [dirty]" if dirty else ""
        lines.append(f"{name:45s} {branch}{status}")

    return "\n".join(lines) if lines else "No repos found."


@mcp.tool()
def search_code(
    pattern: str,
    repo_name: str = "",
    file_glob: str = "*.java",
    case_sensitive: bool = False,
    context_lines: int = 2,
    auth: str = "",
) -> str:
    """
    Search for a pattern (regex) across one repo or all repos.
    Uses ripgrep if available, falls back to grep.

    Args:
        pattern:        Regex or literal string to search for
        repo_name:      Specific repo to search, or empty for all repos
        file_glob:      Glob to limit files (e.g. '*.java', '*.yml', '*.gradle')
        case_sensitive: Default False
        context_lines:  Lines of context around each match
        auth:           Auth token (required only if MCP_AUTH_TOKEN is set)
    """
    auth_err = _check_auth(auth)
    if auth_err:
        return auth_err

    if not pattern or not pattern.strip():
        return "[ERROR] pattern is required"

    repos = {repo_name: _resolve_repo(repo_name)} if repo_name else _all_repos()
    cmd = _build_search_cmd(pattern, file_glob, case_sensitive, context_lines)

    results = []
    for name, path in repos.items():
        out = _run(cmd, path)
        if out.strip():
            results.append(f"=== {name} ===\n{out.strip()}")

    if not results:
        return f"No matches for '{pattern}' in {'all repos' if not repo_name else repo_name}."

    combined = "\n\n".join(results)
    return _truncate(combined, "search results ")


@mcp.tool()
def read_file(
    repo_name: str,
    file_path: str,
    offset: int = 0,
    limit: int = 0,
    auth: str = "",
) -> str:
    """
    Read the contents of a file in a repository. Supports offset/limit for large files.

    Args:
        repo_name:  Repository name (e.g. 'pricing-matrix-service')
        file_path:  Relative path within the repo (e.g. 'src/main/resources/application.properties')
        offset:     Line number to start from (0-based, default 0 = beginning)
        limit:      Max lines to return (0 = entire file)
        auth:       Auth token (required only if MCP_AUTH_TOKEN is set)
    """
    auth_err = _check_auth(auth)
    if auth_err:
        return auth_err

    repo = _resolve_repo(repo_name)
    full_path = _validate_file_path(repo, file_path)

    if not full_path.exists():
        return f"File not found: {file_path}"
    if not full_path.is_file():
        return f"Not a file: {file_path}"

    try:
        max_read_bytes = MAX_OUTPUT * 4
        file_size = full_path.stat().st_size
        if file_size > max_read_bytes:
            with full_path.open("rb") as fh:
                raw = fh.read(max_read_bytes)
            text = raw.decode("utf-8", errors="replace")
        else:
            text = full_path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        total_lines = len(lines)

        if offset > 0 or limit > 0:
            start = min(offset, total_lines)
            end = min(start + limit, total_lines) if limit > 0 else total_lines
            selected = lines[start:end]
            text = "".join(selected)
            header = f"[lines {start + 1}-{start + len(selected)} of {total_lines}]\n"
            return header + _truncate(text, f"{repo_name}/{file_path} ")

        return _truncate(text, f"{repo_name}/{file_path} ")
    except (OSError, UnicodeDecodeError) as e:
        logger.error("Error reading file %s/%s: %s", repo_name, file_path, e)
        return "[ERROR] Could not read the requested file."


@mcp.tool()
def list_files(repo_name: str, pattern: str = "**/*.java", max_results: int = 100, auth: str = "") -> str:
    """
    List files matching a glob pattern in a repository.

    Args:
        repo_name:   Repository name
        pattern:     Glob pattern relative to repo root (e.g. 'src/**/*.java', '**/*.yml')
        max_results: Cap on number of results
        auth:        Auth token (required only if MCP_AUTH_TOKEN is set)
    """
    auth_err = _check_auth(auth)
    if auth_err:
        return auth_err

    repo = _resolve_repo(repo_name)
    matches = sorted(repo.glob(pattern))[:max_results]

    if not matches:
        return f"No files matching '{pattern}' in {repo_name}."

    # Filter to only files within the repo boundary (prevents symlink escapes)
    repo_resolved = repo.resolve()
    safe_matches = [p for p in matches if p.is_file() and p.resolve().is_relative_to(repo_resolved)]

    lines = [str(p.relative_to(repo)) for p in safe_matches]
    result = "\n".join(lines)
    if len(safe_matches) == max_results:
        result += f"\n\n(capped at {max_results} results, use a more specific pattern to narrow down)"
    return result


@mcp.tool()
def git_status(repo_name: str, auth: str = "") -> str:
    """
    Show git status for a repository.

    Args:
        repo_name: Repository name
        auth:      Auth token (required only if MCP_AUTH_TOKEN is set)
    """
    auth_err = _check_auth(auth)
    if auth_err:
        return auth_err

    repo = _resolve_repo(repo_name)
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo).strip()
    status = _run(["git", "status", "--short"], repo)
    log = _run(["git", "log", "--oneline", "-5"], repo)

    return f"Branch: {branch}\n\nStatus:\n{status.strip() or '(clean)'}\n\nRecent commits:\n{log.strip()}"


@mcp.tool()
def git_diff(repo_name: str, target: str = "", auth: str = "") -> str:
    """
    Show git diff for a repository.

    Args:
        repo_name: Repository name
        target:    Empty = unstaged changes, 'staged' = staged changes,
                   branch name = diff vs that branch (e.g. 'main', 'develop')
        auth:      Auth token (required only if MCP_AUTH_TOKEN is set)
    """
    auth_err = _check_auth(auth)
    if auth_err:
        return auth_err

    repo = _resolve_repo(repo_name)

    if target == "staged":
        cmd = ["git", "diff", "--cached"]
    elif target:
        # Validate branch name — alphanumeric, hyphens, slashes, dots, underscores
        if not re.match(r"^[\w./-]+$", target):
            return f"[ERROR] Invalid branch name: '{target}'"
        cmd = ["git", "diff", f"{target}...HEAD"]
    else:
        cmd = ["git", "diff"]

    return _truncate(_run(cmd, repo), "diff ")


@mcp.tool()
def git_log(repo_name: str, count: int = 10, branch: str = "", auth: str = "") -> str:
    """
    Show recent git commits for a repository.

    Args:
        repo_name: Repository name
        count:     Number of commits to show (max 100)
        branch:    Specific branch (defaults to current branch)
        auth:      Auth token (required only if MCP_AUTH_TOKEN is set)
    """
    auth_err = _check_auth(auth)
    if auth_err:
        return auth_err

    repo = _resolve_repo(repo_name)
    count = min(max(count, 1), 100)
    cmd = ["git", "log", "--oneline", f"-{count}"]
    if branch:
        if not re.match(r"^[\w./-]+$", branch):
            return f"[ERROR] Invalid branch name: '{branch}'"
        cmd.append(branch)
    return _run(cmd, repo)


@mcp.tool()
def get_dependencies(repo_name: str, auth: str = "") -> str:
    """
    Read the build file (build.gradle, pom.xml, or package.json) for a repo.

    Args:
        repo_name: Repository name
        auth:      Auth token (required only if MCP_AUTH_TOKEN is set)
    """
    auth_err = _check_auth(auth)
    if auth_err:
        return auth_err

    repo = _resolve_repo(repo_name)

    for build_file in ["build.gradle", "build.gradle.kts", "pom.xml", "package.json", "pyproject.toml",
                        "requirements.txt", "Cargo.toml", "go.mod"]:
        path = repo / build_file
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            return f"=== {build_file} ===\n{_truncate(text, build_file + ' ')}"

    return f"No build file found in {repo_name}."


@mcp.tool()
def run_build(repo_name: str, task: str = "build", extra_args: str = "", auth: str = "") -> str:
    """
    Run a build task in a repository. Supports Gradle, Maven, and npm.

    Args:
        repo_name:  Repository name
        task:       Build task — 'build', 'test', 'clean', 'bootRun', etc.
        extra_args: Extra CLI arguments (e.g. '-x test', '--info')
        auth:       Auth token (required only if MCP_AUTH_TOKEN is set)
    """
    auth_err = _check_auth(auth)
    if auth_err:
        return auth_err

    if task not in _ALLOWED_BUILD_TASKS:
        return (
            f"[ERROR] Task '{task}' is not in the allowed list. "
            f"Allowed: {', '.join(sorted(_ALLOWED_BUILD_TASKS))}"
        )

    try:
        validated_args = _validate_extra_args(extra_args)
    except ValueError as e:
        return f"[ERROR] {e}"

    repo = _resolve_repo(repo_name)

    if (repo / "gradlew").exists():
        cmd = ["./gradlew", task] + validated_args
    elif (repo / "mvnw").exists():
        cmd = ["./mvnw", task] + validated_args
    elif (repo / "pom.xml").exists():
        cmd = ["mvn", task] + validated_args
    elif (repo / "package.json").exists():
        cmd = ["npm", "run", task] + validated_args
    else:
        return f"No recognized build tool found in {repo_name}."

    logger.info("Running build: %s in %s", " ".join(cmd), repo_name)
    return _run(cmd, repo, timeout=300)


@mcp.tool()
def find_repos_with(
    pattern: str,
    file_glob: str = "*.java",
    case_sensitive: bool = False,
    auth: str = "",
) -> str:
    """
    Find which repos contain a given class, method, config key, or pattern.
    Returns only repo names and match counts — use search_code for full matches.
    Uses ripgrep if available, falls back to grep.

    Args:
        pattern:        Text or regex to search for (e.g. 'MatrixIntersection', 'win-webauth')
        file_glob:      File type to search (e.g. '*.java', '*.yml', '*.gradle')
        case_sensitive: Default False
        auth:           Auth token (required only if MCP_AUTH_TOKEN is set)
    """
    auth_err = _check_auth(auth)
    if auth_err:
        return auth_err

    if not pattern or not pattern.strip():
        return "[ERROR] pattern is required"

    repos = _all_repos()
    cmd = _build_search_cmd(pattern, file_glob, case_sensitive, context_lines=0, files_only=True)

    hits = []
    for name, path in repos.items():
        out = _run(cmd, path)
        files = [line for line in out.strip().splitlines() if line and not line.startswith("[")]
        if files:
            hits.append(f"{name}: {len(files)} file(s)")

    if not hits:
        return f"No repos found containing '{pattern}' in {file_glob} files."

    return "\n".join(hits)


# ── Entry point ───────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="repobridge MCP server — expose local Git repos to any AI agent via MCP",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=os.environ.get("MCP_TRANSPORT", _config.get("transport", _DEFAULT_TRANSPORT)),
        help="Transport protocol (default: stdio). Use 'sse' or 'streamable-http' for HTTP-based clients.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MCP_HOST", _config.get("host", _DEFAULT_HOST)),
        help="Host to bind for SSE/HTTP transports (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MCP_PORT", _config.get("port", _DEFAULT_PORT))),
        help=f"Port for SSE/HTTP transports (default: {_DEFAULT_PORT})",
    )
    return parser.parse_args()


def main():
    args = _parse_args()

    logger.info(
        "Starting repobridge MCP server (transport=%s, roots=%d, max_output=%d, cache_ttl=%ds, auth=%s, search=%s)",
        args.transport,
        len(REPO_ROOTS),
        MAX_OUTPUT,
        CACHE_TTL,
        "enabled" if AUTH_TOKEN else "disabled",
        "ripgrep" if _HAS_RIPGREP else "grep",
    )

    mcp.settings.host = args.host
    mcp.settings.port = args.port

    if args.transport in ("sse", "streamable-http"):
        logger.info("Listening on http://%s:%d", args.host, args.port)

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
