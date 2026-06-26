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
import functools
import hmac
import logging
import os
import json
import re
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Protocol, TypeVar

from mcp.server.fastmcp import FastMCP
from repobridge_config import load_roots, discover_repos

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

# ── TTL Cache ─────────────────────────────────────────────────────────────────

_V = TypeVar("_V")


class TTLCache(Generic[_V]):
    """Thread-safe single-value cache with time-to-live expiry."""

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._value: _V | None = None
        self._ts: float = 0.0
        self._lock = threading.Lock()

    def get(self, compute: Callable[[], _V]) -> _V:
        """Return cached value if fresh, otherwise call compute() and cache result."""
        now = time.monotonic()
        with self._lock:
            if self._value is not None and (now - self._ts) < self._ttl:
                return self._value
            self._value = compute()
            self._ts = now
            return self._value

    def invalidate(self) -> None:
        """Evict the cached value."""
        with self._lock:
            self._value = None
            self._ts = 0.0


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
    """Resolve REPO_ROOTS via shared config module (env var > config file > defaults)."""
    roots = load_roots()
    if roots:
        logger.info("Using repo roots: %d directories", len(roots))
        return [str(r) for r in roots]
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

# ── Search backends ───────────────────────────────────────────────────────────


class SearchBackend(Protocol):
    def search(
        self,
        pattern: str,
        repos: dict[str, Path],
        file_glob: str,
        case_sensitive: bool,
        context_lines: int,
    ) -> tuple[dict[str, str], bool]:
        """Search for pattern across repos. Returns ({repo: output}, timed_out)."""
        ...

    def find_files(
        self,
        pattern: str,
        repos: dict[str, Path],
        file_glob: str,
        case_sensitive: bool,
    ) -> tuple[dict[str, int], bool]:
        """Find repos containing pattern. Returns ({repo: file_count}, timed_out)."""
        ...


class RipgrepBackend:
    """Search backend using a single rg pass across all repo roots."""

    def search(
        self,
        pattern: str,
        repos: dict[str, Path],
        file_glob: str,
        case_sensitive: bool,
        context_lines: int,
    ) -> tuple[dict[str, str], bool]:
        resolved = {name: str(path.resolve()) for name, path in repos.items()}
        sorted_repos = sorted(resolved.items(), key=lambda x: len(x[1]), reverse=True)
        all_paths = [rpath for _, rpath in sorted_repos]

        cmd = ["rg", "-n", f"-C{context_lines}", "-g", file_glob]
        if not case_sensitive:
            cmd.append("-i")
        cmd.append(pattern)
        cmd.extend(all_paths)

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                stdin=subprocess.DEVNULL, timeout=60,
            )
            raw = result.stdout
        except (subprocess.TimeoutExpired, OSError):
            return {}, True

        if not raw.strip():
            return {}, False

        buckets: dict[str, list[str]] = {name: [] for name in repos}
        current_repo: str | None = None

        for line in raw.splitlines():
            if line == "--":
                if current_repo:
                    buckets[current_repo].append("--")
                continue
            matched = False
            for name, rpath in sorted_repos:
                prefix = rpath + "/"
                if line.startswith(prefix):
                    current_repo = name
                    line = "./" + line[len(prefix):]
                    matched = True
                    break
            if not matched and line.startswith("/"):
                current_repo = None
            if current_repo:
                buckets[current_repo].append(line)

        hits = {
            name: "\n".join(lines).strip("--\n").strip()
            for name, lines in buckets.items()
            if any(ln != "--" for ln in lines)
        }
        return hits, False

    def find_files(
        self,
        pattern: str,
        repos: dict[str, Path],
        file_glob: str,
        case_sensitive: bool,
    ) -> tuple[dict[str, int], bool]:
        resolved = {name: str(path.resolve()) for name, path in repos.items()}
        sorted_repos = sorted(resolved.items(), key=lambda x: len(x[1]), reverse=True)
        cmd = ["rg", "-l", "-g", file_glob]
        if not case_sensitive:
            cmd.append("-i")
        cmd.append(pattern)
        cmd.extend(rpath for _, rpath in sorted_repos)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                stdin=subprocess.DEVNULL, timeout=60,
            )
            counts: dict[str, int] = {}
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                for name, rpath in sorted_repos:
                    if line.startswith(rpath + "/"):
                        counts[name] = counts.get(name, 0) + 1
                        break
            return counts, False
        except (subprocess.TimeoutExpired, OSError):
            return {}, True


class GrepBackend:
    """Search backend using parallel grep subprocesses - one per repo."""

    def search(
        self,
        pattern: str,
        repos: dict[str, Path],
        file_glob: str,
        case_sensitive: bool,
        context_lines: int,
    ) -> tuple[dict[str, str], bool]:
        cmd = _build_search_cmd(pattern, file_glob, case_sensitive, context_lines)
        results = _search_repos_parallel(repos, cmd)
        timed_out = any("[TIMEOUT" in out for out in results.values())
        return results, timed_out

    def find_files(
        self,
        pattern: str,
        repos: dict[str, Path],
        file_glob: str,
        case_sensitive: bool,
    ) -> tuple[dict[str, int], bool]:
        cmd = _build_search_cmd(pattern, file_glob, case_sensitive, context_lines=0, files_only=True)
        results = _search_repos_parallel(repos, cmd)
        timed_out = any("[TIMEOUT" in out for out in results.values())
        counts: dict[str, int] = {}
        for name, out in results.items():
            if "[TIMEOUT" not in out and not out.startswith("[ERROR"):
                files = [ln for ln in out.strip().splitlines() if ln and not ln.startswith("[")]
                if files:
                    counts[name] = len(files)
        return counts, timed_out


# ── Tool context ─────────────────────────────────────────────────────────────


@dataclass
class ToolContext:
    """Injectable bundle of runtime dependencies used by all MCP tools."""
    max_output: int
    auth_token: str
    backend: SearchBackend


# ── Caching ──────────────────────────────────────────────────────────────────

_IDE_CACHE_TTL = 30.0

_repo_cache: TTLCache[dict[str, Path]] = TTLCache(CACHE_TTL)
_ide_cache: TTLCache[list[tuple[str, str]]] = TTLCache(_IDE_CACHE_TTL)


def _invalidate_cache() -> None:
    _repo_cache.invalidate()


def _detect_ides_cached() -> list[tuple[str, str]]:
    return _ide_cache.get(_detect_ides)


_DEFAULT_BACKEND: SearchBackend = RipgrepBackend() if _HAS_RIPGREP else GrepBackend()

_ctx = ToolContext(
    max_output=MAX_OUTPUT,
    auth_token=AUTH_TOKEN,
    backend=_DEFAULT_BACKEND,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _all_repos() -> dict[str, Path]:
    """Return {repo_name: repo_path} for all git repos under REPO_ROOTS.

    Results are cached for CACHE_TTL seconds.
    """
    def _discover() -> dict[str, Path]:
        repos = discover_repos([Path(r) for r in REPO_ROOTS])
        logger.debug("Discovered %d repositories", len(repos))
        return repos

    return _repo_cache.get(_discover)


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
    if not _ctx.auth_token:
        return None
    if not hmac.compare_digest(token, _ctx.auth_token):
        return "[AUTH ERROR] Invalid or missing auth token."
    return None


def _require_auth(f):
    """Decorator: check auth before calling the tool. Safe with FastMCP (follows __wrapped__)."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        auth_err = _check_auth(kwargs.get("auth", ""))
        if auth_err:
            return auth_err
        return f(*args, **kwargs)
    return wrapper


# ── IDE detection ─────────────────────────────────────────────────────────────

_IDE_PORTS = {
    "IntelliJ IDEA": [63342, 63343, 63344],
    "VS Code":       [3000, 6010, 6011],
    "Cursor":        [3000, 6010],
    "Windsurf":      [3000, 6010],
}

_IDE_PROCESSES = {
    "IntelliJ IDEA": ["idea", "IntelliJ IDEA"],
    "VS Code":       ["code", "Code"],
    "Cursor":        ["cursor", "Cursor"],
    "Windsurf":      ["windsurf", "Windsurf"],
}

_IDE_MCP_TOOLS = {
    "IntelliJ IDEA": "ide_find_references, ide_find_definition, ide_find_implementations, ide_search_text, ide_call_hierarchy",
    "VS Code":       "ide_find_references, ide_find_definition (if MCP extension installed)",
    "Cursor":        "built-in symbol search via @ context",
    "Windsurf":      "built-in symbol search via Cascade",
}


def _running_processes() -> list[str]:
    """Return list of running process names (cached per call)."""
    try:
        out = subprocess.run(
            ["ps", "-axco", "comm"],
            capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=5,
        ).stdout
        return out.splitlines()
    except Exception:
        return []


def _port_open(port: int) -> bool:
    """Check if a local TCP port is listening."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _detect_ides() -> list[tuple[str, str]]:
    """
    Detect running IDEs by process name and port.
    Returns list of (ide_name, detection_method) tuples.
    """
    procs = set(_running_processes())
    found = []
    for ide, names in _IDE_PROCESSES.items():
        # process match
        if any(n in procs for n in names):
            found.append((ide, "process"))
            continue
        # port match
        ports = _IDE_PORTS.get(ide, [])
        if any(_port_open(p) for p in ports):
            found.append((ide, "port"))
    return found



def _search_repos_parallel(
    repos: dict[str, Path],
    cmd: list[str],
    files_only: bool = False,
) -> dict[str, str]:
    """Run cmd in each repo concurrently. Returns {repo_name: output}."""
    results: dict[str, str] = {}

    def _search_one(name: str, path: Path) -> tuple[str, str]:
        return name, _run(cmd, path)

    max_workers = min(len(repos), (os.cpu_count() or 4) * 2)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_search_one, name, path): name for name, path in repos.items()}
        for future in as_completed(futures):
            repo_name = futures[future]
            try:
                name, out = future.result()
                results[name] = out
            except Exception:
                results[repo_name] = "[ERROR] search failed"

    return results


def _run(cmd: list[str], cwd: Path, timeout: int = 60) -> str:
    """Run a subprocess command and return its combined output."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        return output[:_ctx.max_output] if len(output) > _ctx.max_output else output
    except subprocess.TimeoutExpired:
        logger.warning("Command timed out after %ds: %s", timeout, " ".join(cmd))
        return f"[TIMEOUT after {timeout}s]"
    except FileNotFoundError:
        logger.error("Command not found: %s", cmd[0])
        return f"[ERROR] Command not found: {cmd[0]}"
    except OSError as e:
        logger.error("OS error running command %s: %s", cmd[0], e)
        return f"[ERROR] {e}"


_GRAPHIFY_HINT = (
    "\n[TIP] Search timed out. If graphify is installed, run /graphify on this "
    "repo for a pre-indexed knowledge graph you can query without live grep."
)


def _truncate(text: str, label: str = "") -> str:
    """Truncate text to _ctx.max_output characters with an indicator."""
    if len(text) > _ctx.max_output:
        return text[:_ctx.max_output] + f"\n\n... [{label}truncated at {_ctx.max_output} chars]"
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
        cmd.append(".")  # explicit path - prevents rg from reading stdin when not a tty
    else:
        cmd = ["grep", "-rn" if not files_only else "-rl", f"--include={file_glob}"]
        if not files_only:
            cmd.append(f"-C{context_lines}")
        if not case_sensitive:
            cmd.append("-i")
        cmd.append(pattern)
        cmd.append(".")  # explicit path - prevents grep stdin blocking on macOS BSD grep
    return cmd


# ── MCP Server ────────────────────────────────────────────────────────────────

mcp = FastMCP("repobridge")


@mcp.tool()
@_require_auth
def get_ide_status(auth: str = "") -> str:
    """
    Detect which IDEs are currently running and which MCP tools they provide.

    Use this before search_code when doing symbol lookups (class names, method
    references, interface implementations). IDE tools give semantic results;
    search_code gives text matches.

    Returns: running IDEs, the MCP tools they expose, and when to prefer them
    over search_code.
    """
    ides = _detect_ides_cached()
    if not ides:
        return (
            "No IDE detected. Using text search (ripgrep/grep) via search_code.\n"
            "Open IntelliJ IDEA, VS Code, Cursor, or Windsurf for semantic symbol search."
        )

    lines = ["IDE(s) detected - prefer IDE MCP tools over search_code for symbol lookups:\n"]
    for ide, method in ides:
        tools = _IDE_MCP_TOOLS.get(ide, "check IDE MCP plugin docs")
        lines.append(f"  {ide} (detected via {method})")
        lines.append(f"    Tools: {tools}")
        lines.append("")

    lines.append("When to use IDE tools vs search_code:")
    lines.append("  ide_find_references     - all callers of a method/class (semantic, handles generics)")
    lines.append("  ide_find_definition     - go to declaration")
    lines.append("  ide_find_implementations - all classes implementing an interface")
    lines.append("  ide_search_text         - text search scoped to open project")
    lines.append("  search_code             - cross-repo text/regex when IDE is not open or pattern is not a symbol")
    return "\n".join(lines)


@mcp.tool()
@_require_auth
def list_repos(filter: str = "", auth: str = "") -> str:
    """
    List all available repositories with their current branch and dirty status.

    Args:
        filter: Optional substring to filter repo names (e.g. 'pricing', 'service')
        auth:   Auth token (required only if MCP_AUTH_TOKEN is set)
    """
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
@_require_auth
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
    if not pattern or not pattern.strip():
        return "[ERROR] pattern is required"

    # Check for running IDEs - semantic tools beat text search for symbol lookups
    ide_hint = ""
    ides = _detect_ides_cached()
    if ides:
        ide_names = ", ".join(ide for ide, _ in ides)
        ide_hint = (
            f"[IDE DETECTED: {ide_names}] "
            "For symbol/class/method lookups prefer ide_find_references or ide_find_definition "
            "over this text search - they use the compiler index and handle generics/inheritance. "
            "Text search results follow:\n\n"
        )

    repos = {repo_name: _resolve_repo(repo_name)} if repo_name else _all_repos()
    hits, timed_out = _ctx.backend.search(
        pattern, repos, file_glob, case_sensitive, context_lines
    )

    results = []
    for name, out in sorted(hits.items()):
        if "[TIMEOUT" in out:
            results.append(f"=== {name} ===\n{out.strip()}")
        elif out.strip():
            results.append(f"=== {name} ===\n{out.strip()}")

    if not results:
        return (
            ide_hint
            + f"No matches for '{pattern}' in {'all repos' if not repo_name else repo_name}."
        )

    combined = "\n\n".join(results)
    result = _truncate(combined, "search results ")
    if timed_out:
        result += _GRAPHIFY_HINT
    return ide_hint + result


@mcp.tool()
@_require_auth
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
    repo = _resolve_repo(repo_name)
    full_path = _validate_file_path(repo, file_path)

    if not full_path.exists():
        return f"File not found: {file_path}"
    if not full_path.is_file():
        return f"Not a file: {file_path}"

    try:
        max_read_bytes = _ctx.max_output * 4
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
@_require_auth
def list_files(repo_name: str, pattern: str = "**/*.java", max_results: int = 100, auth: str = "") -> str:
    """
    List files matching a glob pattern in a repository.

    Args:
        repo_name:   Repository name
        pattern:     Glob pattern relative to repo root (e.g. 'src/**/*.java', '**/*.yml')
        max_results: Cap on number of results
        auth:        Auth token (required only if MCP_AUTH_TOKEN is set)
    """
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
@_require_auth
def git_status(repo_name: str, auth: str = "") -> str:
    """
    Show git status for a repository.

    Args:
        repo_name: Repository name
        auth:      Auth token (required only if MCP_AUTH_TOKEN is set)
    """
    repo = _resolve_repo(repo_name)
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo).strip()
    status = _run(["git", "status", "--short"], repo)
    log = _run(["git", "log", "--oneline", "-5"], repo)

    return f"Branch: {branch}\n\nStatus:\n{status.strip() or '(clean)'}\n\nRecent commits:\n{log.strip()}"


@mcp.tool()
@_require_auth
def git_diff(repo_name: str, target: str = "", auth: str = "") -> str:
    """
    Show git diff for a repository.

    Args:
        repo_name: Repository name
        target:    Empty = unstaged changes, 'staged' = staged changes,
                   branch name = diff vs that branch (e.g. 'main', 'develop')
        auth:      Auth token (required only if MCP_AUTH_TOKEN is set)
    """
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
@_require_auth
def git_log(repo_name: str, count: int = 10, branch: str = "", auth: str = "") -> str:
    """
    Show recent git commits for a repository.

    Args:
        repo_name: Repository name
        count:     Number of commits to show (max 100)
        branch:    Specific branch (defaults to current branch)
        auth:      Auth token (required only if MCP_AUTH_TOKEN is set)
    """
    repo = _resolve_repo(repo_name)
    count = min(max(count, 1), 100)
    cmd = ["git", "log", "--oneline", f"-{count}"]
    if branch:
        if not re.match(r"^[\w./-]+$", branch):
            return f"[ERROR] Invalid branch name: '{branch}'"
        cmd.append(branch)
    return _run(cmd, repo)


@mcp.tool()
@_require_auth
def get_dependencies(repo_name: str, auth: str = "") -> str:
    """
    Read the build file (build.gradle, pom.xml, or package.json) for a repo.

    Args:
        repo_name: Repository name
        auth:      Auth token (required only if MCP_AUTH_TOKEN is set)
    """
    repo = _resolve_repo(repo_name)

    for build_file in ["build.gradle", "build.gradle.kts", "pom.xml", "package.json", "pyproject.toml",
                        "requirements.txt", "Cargo.toml", "go.mod"]:
        path = repo / build_file
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            return f"=== {build_file} ===\n{_truncate(text, build_file + ' ')}"

    return f"No build file found in {repo_name}."


@mcp.tool()
@_require_auth
def run_build(repo_name: str, task: str = "build", extra_args: str = "", auth: str = "") -> str:
    """
    Run a build task in a repository. Supports Gradle, Maven, and npm.

    Args:
        repo_name:  Repository name
        task:       Build task - 'build', 'test', 'clean', 'bootRun', etc.
        extra_args: Extra CLI arguments (e.g. '-x test', '--info')
        auth:       Auth token (required only if MCP_AUTH_TOKEN is set)
    """
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
@_require_auth
def find_repos_with(
    pattern: str,
    file_glob: str = "*.java",
    case_sensitive: bool = False,
    auth: str = "",
) -> str:
    """
    Find which repos contain a given class, method, config key, or pattern.
    Returns only repo names and match counts - use search_code for full matches.
    Uses ripgrep if available, falls back to grep.

    Args:
        pattern:        Text or regex to search for (e.g. 'MatrixIntersection', 'win-webauth')
        file_glob:      File type to search (e.g. '*.java', '*.yml', '*.gradle')
        case_sensitive: Default False
        auth:           Auth token (required only if MCP_AUTH_TOKEN is set)
    """
    if not pattern or not pattern.strip():
        return "[ERROR] pattern is required"

    repos = _all_repos()
    counts, timed_out = _ctx.backend.find_files(pattern, repos, file_glob, case_sensitive)

    hits = [f"{name}: {count} file(s)" for name, count in sorted(counts.items())]

    if not hits:
        return f"No repos found containing '{pattern}' in {file_glob} files."

    result = "\n".join(hits)
    if timed_out:
        result += (
            "\n[TIP] Some repos timed out. If graphify is installed, run /graphify on "
            "those repos for pre-indexed search."
        )
    return result


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
        _ctx.max_output,
        CACHE_TTL,
        "enabled" if _ctx.auth_token else "disabled",
        "ripgrep" if isinstance(_ctx.backend, RipgrepBackend) else "grep",
    )

    mcp.settings.host = args.host
    mcp.settings.port = args.port

    if args.transport in ("sse", "streamable-http"):
        logger.info("Listening on http://%s:%d", args.host, args.port)

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
