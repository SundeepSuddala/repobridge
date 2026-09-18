# repobridge

An MCP (Model Context Protocol) server that gives any AI agent access to search, read, and work across all your local Git repositories at once.

Works with **Claude Code**, **Claude Desktop**, **GitHub Copilot**, **Gemini**, **Cursor**, **Windsurf**, **Cline**, and any other MCP-compatible client.

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/sundeepsuddala/repobridge.git
cd repobridge
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. Configure your repo folders
cp example-config.json ~/.repobridge.json
# Edit ~/.repobridge.json - set repo_roots to YOUR directories

# 3. Add to your AI tool (see "Connecting Your AI Agent" below)

# 4. Done - your AI agent can now search your repos
```

### One-line install for Claude Code (recommended)

Instead of the manual steps above, run:

```bash
./install.sh
```

This single script:
- Creates the Python venv and installs repobridge
- Prompts for your repo root directories and writes `~/.repobridge.json`
  (if you skip, a hook will prompt Claude to configure it automatically on first use)
- Registers the MCP server at **user scope** so it works in every project
- Installs two PreToolUse hooks into `~/.claude/settings.json`:

| Hook | Trigger | Action |
|---|---|---|
| `repobridge-nudge` | Any `grep`/`find`/`cat`/`ls`/`git -C` aimed at *another* repo | Asks Claude to use `mcp__repobridge__*` tools instead |
| `repobridge-ensure-roots` | Any `mcp__repobridge__*` tool call | If no roots configured, asks Claude to collect and write them |

**The nudge hook uses `permissionDecision: "ask"`** - it surfaces a prompt,
never hard-blocks. False positives cost one click; write commands and the current
project directory are never intercepted.

**Restart Claude Code** after running `./install.sh` to load the hook and MCP server.

#### Uninstall

```bash
./uninstall.sh
```

Removes the two hook entries from `~/.claude/settings.json`, unregisters the MCP
server, and (with confirmation) optionally deletes `~/.repobridge.json` and `.venv`.
A backup of `settings.json` is created at `settings.json.bak` before any changes.

## Requirements

- Python 3.11+
- Git
- `grep` (built-in) or [ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) for faster search

Optional (for `run_build`): Gradle / Maven / npm depending on your projects.

---

## Configuration

The server looks for settings in this order: **CLI args > environment variables > config file > defaults**.

### Step 1: Create config file

```bash
cp example-config.json ~/.repobridge.json
```

Edit `~/.repobridge.json`:

```json
{
  "repo_roots": [
    "~/projects/backend",
    "~/projects/frontend",
    "~/projects/libraries"
  ],
  "max_output": 20000,
  "cache_ttl_seconds": 300,
  "auth_token": "",
  "github_org": "",
  "clone_idle_days": 7
}
```

**`repo_roots`** is the only required setting. It lists directories that contain your Git repos. The server scans one level deep for directories with a `.git` folder:

```
~/projects/backend/         ← this is a "repo root"
├── user-service/           ← detected as a repo (has .git/)
├── order-service/          ← detected as a repo
└── shared-libs/            ← detected as a repo

~/projects/frontend/        ← another repo root
├── web-app/
└── mobile-app/
```

### Alternative: Environment variables

```bash
export REPOBRIDGE_ROOTS="~/projects/backend:~/projects/frontend"
export REPOBRIDGE_MAX_OUTPUT=40000
export MCP_AUTH_TOKEN="my-secret-token"   # optional
export MCP_TRANSPORT="stdio"              # stdio | sse | streamable-http
export MCP_HOST="127.0.0.1"              # for sse/streamable-http
export MCP_PORT="7200"                   # for sse/streamable-http
export LOG_LEVEL="DEBUG"                 # DEBUG | INFO | WARNING | ERROR
```

### Settings reference

| Setting | Env var | Config key | CLI flag | Default |
|---------|---------|------------|----------|---------|
| Repo roots | `REPOBRIDGE_ROOTS` | `repo_roots` | — | `[]` (empty) |
| Max output | `REPOBRIDGE_MAX_OUTPUT` | `max_output` | — | 20,000 chars |
| Cache TTL | — | `cache_ttl_seconds` | — | 300s |
| Auth token | `MCP_AUTH_TOKEN` | `auth_token` | — | `""` (disabled) |
| GitHub org | `REPOBRIDGE_GITHUB_ORG` | `github_org` | — | linked `gh` account's own org |
| Clone idle eviction | `REPOBRIDGE_CLONE_IDLE_DAYS` | `clone_idle_days` | — | 7 days |
| Transport | `MCP_TRANSPORT` | `transport` | `--transport` | `stdio` |
| Host | `MCP_HOST` | `host` | `--host` | `127.0.0.1` |
| Port | `MCP_PORT` | `port` | `--port` | `7200` |
| Log level | `LOG_LEVEL` | — | — | `INFO` |

---

## Connecting Your AI Agent

### Claude Code (CLI)

Claude Code auto-starts MCP servers at session startup — no manual launch needed. Add repobridge once and it will be available in every conversation.

#### Option A: Global (recommended) — available in every Claude Code session

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "repobridge": {
      "command": "/absolute/path/to/repobridge/.venv/bin/python",
      "args": ["/absolute/path/to/repobridge/server.py"]
    }
  }
}
```

Claude Code picks this up automatically. You can verify it loaded by running `/mcp` in any session — `repobridge` should appear in the list.

#### Option B: Project-level — only starts when opening a specific project

Add a `.mcp.json` file in your project root:

```json
{
  "mcpServers": {
    "repobridge": {
      "command": "/absolute/path/to/repobridge/.venv/bin/python",
      "args": ["/absolute/path/to/repobridge/server.py"]
    }
  }
}
```

This repo ships a `.mcp.json` pointed at its own server — useful if you clone repobridge and want it to auto-start when working inside that directory.

> Always use the **full path** to the venv Python so dependencies resolve correctly.

---

### Claude Desktop

Add to your `claude_desktop_config.json`:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "repobridge": {
      "command": "/absolute/path/to/repobridge/.venv/bin/python",
      "args": ["/absolute/path/to/repobridge/server.py"]
    }
  }
}
```

---

### GitHub Copilot (VS Code)

GitHub Copilot supports MCP servers via VS Code settings. Add to your `.vscode/settings.json` (project) or user settings:

```json
{
  "github.copilot.chat.mcpServers": {
    "repobridge": {
      "command": "/absolute/path/to/repobridge/.venv/bin/python",
      "args": ["/absolute/path/to/repobridge/server.py"]
    }
  }
}
```

Alternatively, create a `.vscode/mcp.json` in your project:

```json
{
  "servers": {
    "repobridge": {
      "command": "/absolute/path/to/repobridge/.venv/bin/python",
      "args": ["/absolute/path/to/repobridge/server.py"]
    }
  }
}
```

After adding, open Copilot Chat and the tools will be available. You can verify by typing `@repobridge` in the chat.

---

### Cursor

Add to your `.cursor/mcp.json` (project-level) or `~/.cursor/mcp.json` (global):

```json
{
  "mcpServers": {
    "repobridge": {
      "command": "/absolute/path/to/repobridge/.venv/bin/python",
      "args": ["/absolute/path/to/repobridge/server.py"]
    }
  }
}
```

After adding, restart Cursor. The tools appear in the AI chat under the MCP tools list.

---

### Windsurf

Add to `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "repobridge": {
      "command": "/absolute/path/to/repobridge/.venv/bin/python",
      "args": ["/absolute/path/to/repobridge/server.py"]
    }
  }
}
```

---

### Gemini (Google AI Studio / Gemini CLI)

Gemini supports MCP servers. Add to your `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "repobridge": {
      "command": "/absolute/path/to/repobridge/.venv/bin/python",
      "args": ["/absolute/path/to/repobridge/server.py"]
    }
  }
}
```

For Gemini CLI, you can also pass the MCP server when starting:

```bash
gemini --mcp "repobridge:/absolute/path/to/repobridge/.venv/bin/python /absolute/path/to/repobridge/server.py"
```

---

### Cline (VS Code Extension)

Go to Cline settings > MCP Servers > Add New, or edit `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`:

```json
{
  "mcpServers": {
    "repobridge": {
      "command": "/absolute/path/to/repobridge/.venv/bin/python",
      "args": ["/absolute/path/to/repobridge/server.py"],
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

---

### HTTP/SSE Mode (Local or Web Clients)

For clients that connect via HTTP instead of stdio, run the server in SSE or streamable-http mode:

```bash
# SSE mode (widely supported)
python server.py --transport sse --host localhost --port 7200

# Streamable HTTP mode (newer, recommended - required by enterprise
# MCP policies that only allow servers reachable via localhost)
python server.py --transport streamable-http --host localhost --port 7200
```

Then point your MCP client to `http://localhost:7200/sse` (SSE) or `http://localhost:7200/mcp` (streamable-http).

Only bind to `0.0.0.0` (or another non-loopback address) if you specifically need remote access and understand the security implications - the server has no built-in TLS or network-level auth.

You can also set these in `~/.repobridge.json`:

```json
{
  "repo_roots": ["~/projects"],
  "transport": "streamable-http",
  "host": "localhost",
  "port": 7200
}
```

---

### Any MCP-Compatible Client

The server follows the [MCP specification](https://modelcontextprotocol.io). Any client that supports MCP can connect using:

- **stdio transport** (default): spawn the server as a subprocess
- **SSE transport**: connect to `http://host:port/sse`
- **Streamable HTTP transport**: connect to `http://host:port/mcp`

The generic stdio configuration for any client:

```json
{
  "command": "/absolute/path/to/repobridge/.venv/bin/python",
  "args": ["/absolute/path/to/repobridge/server.py"]
}
```

With environment variables:

```json
{
  "command": "/absolute/path/to/repobridge/.venv/bin/python",
  "args": ["/absolute/path/to/repobridge/server.py"],
  "env": {
    "REPOBRIDGE_ROOTS": "~/work/backend:~/work/frontend",
    "REPOBRIDGE_MAX_OUTPUT": "40000"
  }
}
```

---

## Tools

### `list_repos`

List all repositories with current branch and dirty status.

```
list_repos(filter="pricing")
```

| Param | Default | Description |
|-------|---------|-------------|
| `filter` | `""` | Substring to filter repo names |

---

### `search_code`

Search for a regex pattern across one or all repos. Uses ripgrep if installed.

```
search_code(pattern="@EnableWebSecurity", file_glob="*.java")
search_code(pattern="spring.datasource", repo_name="user-service", file_glob="*.yml")
```

| Param | Default | Description |
|-------|---------|-------------|
| `pattern` | *required* | Regex or literal string |
| `repo_name` | `""` | Specific repo, or empty for all |
| `file_glob` | `"*.java"` | File filter |
| `case_sensitive` | `False` | Case sensitivity |
| `context_lines` | `2` | Lines of context around matches |

---

### `read_file`

Read a file from a repo with optional line-range pagination.

```
read_file(repo_name="user-service", file_path="src/main/resources/application.yml")
read_file(repo_name="user-service", file_path="big-file.log", offset=100, limit=50)
```

| Param | Default | Description |
|-------|---------|-------------|
| `repo_name` | *required* | Repository name |
| `file_path` | *required* | Relative path within the repo |
| `offset` | `0` | Start line (0-based) |
| `limit` | `0` | Max lines (0 = entire file) |

---

### `list_files`

List files matching a glob pattern in a repo.

```
list_files(repo_name="user-service", pattern="**/*.yml")
```

| Param | Default | Description |
|-------|---------|-------------|
| `repo_name` | *required* | Repository name |
| `pattern` | `"**/*.java"` | Glob pattern |
| `max_results` | `100` | Max files returned |

---

### `git_status`

Show branch, uncommitted changes, and last 5 commits.

```
git_status(repo_name="user-service")
```

---

### `git_diff`

Show diff — unstaged, staged, or vs a branch.

```
git_diff(repo_name="user-service")                  # unstaged
git_diff(repo_name="user-service", target="staged")  # staged
git_diff(repo_name="user-service", target="main")    # vs main
```

| Param | Default | Description |
|-------|---------|-------------|
| `repo_name` | *required* | Repository name |
| `target` | `""` | `""` = unstaged, `"staged"` = cached, or branch name |

---

### `git_log`

Show recent commits (max 100).

```
git_log(repo_name="user-service", count=20)
git_log(repo_name="user-service", branch="develop")
```

| Param | Default | Description |
|-------|---------|-------------|
| `repo_name` | *required* | Repository name |
| `count` | `10` | Number of commits |
| `branch` | `""` | Branch (defaults to current) |

---

### `get_dependencies`

Read the build/dependency file. Supports: `build.gradle`, `build.gradle.kts`, `pom.xml`, `package.json`, `pyproject.toml`, `requirements.txt`, `Cargo.toml`, `go.mod`.

```
get_dependencies(repo_name="user-service")
```

---

### `run_build`

Run a build task. Auto-detects Gradle/Maven/npm.

```
run_build(repo_name="user-service", task="test")
run_build(repo_name="user-service", task="build", extra_args="-x test --info")
```

| Param | Default | Description |
|-------|---------|-------------|
| `repo_name` | *required* | Repository name |
| `task` | `"build"` | Task name |
| `extra_args` | `""` | Additional CLI arguments |

Allowed tasks: `assemble`, `bootJar`, `bootRun`, `build`, `check`, `clean`, `compile`, `compileJava`, `compileKotlin`, `dev`, `format`, `install`, `jar`, `lint`, `package`, `site`, `start`, `test`, `typecheck`, `verify`.

---

### `find_repos_with`

Find which repos contain a pattern. Returns repo names and counts only.

```
find_repos_with(pattern="OktaAuth", file_glob="*.java")
find_repos_with(pattern="react-router", file_glob="package.json")
```

| Param | Default | Description |
|-------|---------|-------------|
| `pattern` | *required* | Text or regex |
| `file_glob` | `"*.java"` | File type filter |
| `case_sensitive` | `False` | Case sensitivity |

---

### `clone_github_repo`

Every tool above only sees repos already cloned under `repo_roots`. When a
repo isn't there (e.g. it only exists on GitHub), the "not found" error names
your linked org and points at this tool - clone it once, then every other
tool works on it exactly like a local repo, no extra setup.

Clones live in `~/.repobridge/remote-clones/` (safe to delete anytime - repos
just get re-cloned on demand) and are pruned automatically after
`clone_idle_days` of no use.

This makes a network call and writes to disk - confirm with the user before
cloning a repo you weren't explicitly told to.

```
clone_github_repo(repo_name="mainframe-gateway")
clone_github_repo(repo_name="mainframe-gateway", org="winsupplyinc", depth=1)
```

| Param | Default | Description |
|-------|---------|-------------|
| `repo_name` | *required* | Repository name, no path separators |
| `org` | `""` | GitHub org/owner (default: resolved from config/env/linked `gh` account) |
| `depth` | `0` | Shallow-clone depth for large repos (`0` = full history) |

---

### `get_ide_status`

Detect which IDEs are running and which semantic MCP tools they expose. Call this before `search_code` when looking up symbols - IDE tools give semantic results (handles generics, overloads, cross-module resolution) while `search_code` gives text matches.

```
get_ide_status()
```

No parameters. Returns a list of running IDEs, their available MCP tools, and guidance on when to prefer IDE tools over `search_code`.

---

## Security

- **Path traversal prevention**: `read_file` and `list_files` validate that resolved paths stay within repo boundaries using `Path.is_relative_to()`.
- **Command injection prevention**: `run_build` only accepts whitelisted task names and validates extra arguments against a strict character allowlist. Dangerous flags like `--init-script` are blocked.
- **Branch name validation**: `git_diff` and `git_log` validate branch names to prevent injection.
- **Timing-safe auth**: Token comparison uses `hmac.compare_digest()` to prevent timing side-channel attacks.
- **Thread-safe caching**: Repository cache is protected by a threading lock.
- **OOM prevention**: Large files are read in bounded chunks, not loaded entirely into memory.
- **No secrets in defaults**: The config file and defaults contain no credentials.

## Architecture

```
repobridge_config.py    — shared: load_roots(), has_roots(), discover_repos()
server.py
├── CLI & Transport     - stdio, SSE, or streamable-http (configurable)
├── Logging             - structured logging via Python logging module
├── Configuration       - CLI args > env vars > config file > defaults
├── ToolContext         - injectable bundle: max_output, auth_token, backend
├── SearchBackend       - Protocol + RipgrepBackend / GrepBackend implementations
├── TTLCache            - generic thread-safe TTL cache (repo discovery, IDE detection)
├── Input validation    - path traversal, command injection, branch names
├── Auth                - @_require_auth decorator, timing-safe hmac.compare_digest
├── Helpers             - _all_repos(), _resolve_repo(), _run(), _truncate()
├── 11 MCP Tools        - @mcp.tool() + @_require_auth decorated functions
└── Entry point         - main() with argparse → mcp.run(transport=...)
hooks/
├── repobridge-nudge.py         - PreToolUse: redirect cross-repo bash to MCP tools
└── repobridge-ensure-roots.py  - PreToolUse: prompt for config if no roots set
```

## Troubleshooting

**"No repositories found"**
- Check that `repo_roots` in `~/.repobridge.json` points to directories that exist
- Each repo must have a `.git/` directory (the server scans only one level deep)
- Run `LOG_LEVEL=DEBUG python server.py` to see what's being scanned

**"Repo 'x' not found"**
- Run `list_repos()` to see all discovered repos
- Repo names are the directory names, not the Git remote names

**Search is slow**
- Install [ripgrep](https://github.com/BurntSushi/ripgrep): `brew install ripgrep` (macOS) or `apt install ripgrep` (Ubuntu)

**"Command not found" errors**
- Make sure `git` is in your PATH
- For `run_build`, ensure the required build tool is installed

**Tools not showing in your AI client**
- Verify you used the **absolute path** to the venv Python, not just `python`
- Restart your AI client after adding the MCP config
- Check the server log output for errors: `LOG_LEVEL=DEBUG python server.py`

**SSE/HTTP mode not connecting**
- Make sure the port isn't in use: `lsof -i :7200`
- For remote access, use `--host 0.0.0.0` (not the default `127.0.0.1`)
- Check firewall settings if connecting from another machine

## License

MIT
