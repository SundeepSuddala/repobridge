# Architectural Refactor - 2026-06-26

## Scope

Five improvements identified by `/improve-codebase-architecture`. Tests baseline: 60/60 green.

---

## Plan

- [x] **Phase 1 - TTLCache consolidation** (3 variables -> 1 object, fixes ide cache test pollution)
  - Add `TTLCache[V]` generic class with `get(compute)` and `invalidate()` methods
  - Replace `_repo_cache / _repo_cache_time / _repo_cache_lock` triplet
  - Replace `_ide_cache / _ide_cache_time / _ide_cache_lock` triplet
  - Update `_all_repos()`, `_detect_ides_cached()`, `_invalidate_cache()`
  - Update `clear_module_cache` test fixture to call `.invalidate()` on both caches
  - Run tests green

- [x] **Phase 2 - `@_require_auth` decorator** (removes 11 identical auth-check blocks)
  - Verified safe: FastMCP uses `inspect.signature(func, eval_str=True)` which follows `__wrapped__`
  - Add `_require_auth(f)` using `functools.wraps` + `**kwargs` pass-through
  - Apply to all 10 `@mcp.tool()` functions
  - Run tests green

- [x] **Phase 3 - `SearchBackend` abstraction** (decouples ripgrep from tool logic)
  - Add `SearchBackend` Protocol with `search(pattern, paths, timeout) -> dict[str, str]`
  - Extract `RipgrepBackend` and `GrepBackend` implementing the Protocol
  - Refactor `search_code()` and `find_repos_with()` to accept a backend
  - Module-level `_DEFAULT_BACKEND` selected at startup (same `_HAS_RIPGREP` logic)
  - Run tests green

- [x] **Phase 4 - Shared config module** (eliminates duplicate config in server + 2 hooks)
  - Create `repobridge_config.py` at repo root with `load_roots()` and `discover_repos()`
  - server.py imports from it
  - Both hooks add `sys.path.insert(0, SCRIPT_DIR_PARENT)` then import `repobridge_config`
  - Run tests + smoke-test both hooks green

- [x] **Phase 5 - `ToolContext` dependency injection** (removes 13 module-global dependencies)
  - Add `ToolContext` dataclass: `repos`, `max_output`, `auth_token`, `backend`, `truncate()`
  - Create `_make_context()` for production use
  - Refactor all 10 tools to accept `ctx: ToolContext` (hidden from MCP schema via `Context` exclusion pattern)
  - Tests inject `ToolContext` directly - no more module-global patching

---

## Risk / notes

- Phases 1-2: purely mechanical, zero behavior change, safe to merge independently
- Phase 3: search logic extracted but same subprocess calls - behavior preserved
- Phase 4: shared module importable from hooks using `sys.path.insert` (system python path)
- Phase 5: largest change; all 10 tools touched. Will keep `@_require_auth` decorator even after DI (auth still best as decorator, not inside ctx)

## Progress notes

## Review
