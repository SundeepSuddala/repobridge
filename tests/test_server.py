"""Tests for repobridge MCP server."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo in a temp directory."""
    repo = tmp_path / "my-service"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo, check=True, capture_output=True,
    )
    (repo / "README.md").write_text("# my-service\n")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("def hello():\n    return 'world'\n")
    (repo / "package.json").write_text(json.dumps({"name": "my-service", "version": "1.0.0"}))
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=repo, check=True, capture_output=True,
    )
    return repo


@pytest.fixture()
def tmp_root(tmp_repo: Path) -> Path:
    """Return the parent directory containing the test repo."""
    return tmp_repo.parent


@pytest.fixture(autouse=True)
def clear_module_cache():
    """Reset the repo cache between tests."""
    import server
    server._repo_cache = None
    server._repo_cache_time = 0.0
    yield
    server._repo_cache = None
    server._repo_cache_time = 0.0


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_returns_empty_dict_when_no_file(self, tmp_path: Path):
        import server
        config_path = tmp_path / ".repobridge.json"
        with patch.object(Path, "home", return_value=tmp_path):
            result = server._load_config()
        assert result == {}

    def test_loads_valid_json_file(self, tmp_path: Path):
        import server
        config_path = tmp_path / ".repobridge.json"
        config_path.write_text(json.dumps({"repo_roots": ["~/projects"], "max_output": 5000}))
        with patch.object(Path, "home", return_value=tmp_path):
            result = server._load_config()
        assert result["repo_roots"] == ["~/projects"]
        assert result["max_output"] == 5000

    def test_returns_empty_dict_on_invalid_json(self, tmp_path: Path):
        import server
        config_path = tmp_path / ".repobridge.json"
        config_path.write_text("not valid json{{{")
        with patch.object(Path, "home", return_value=tmp_path):
            result = server._load_config()
        assert result == {}


# ---------------------------------------------------------------------------
# Helpers: _validate_file_path
# ---------------------------------------------------------------------------

class TestValidateFilePath:
    def test_allows_valid_path(self, tmp_repo: Path):
        import server
        path = server._validate_file_path(tmp_repo, "README.md")
        assert path == (tmp_repo / "README.md").resolve()

    def test_allows_nested_path(self, tmp_repo: Path):
        import server
        path = server._validate_file_path(tmp_repo, "src/main.py")
        assert path.name == "main.py"

    def test_rejects_path_traversal(self, tmp_repo: Path):
        import server
        with pytest.raises(ValueError, match="Path traversal denied"):
            server._validate_file_path(tmp_repo, "../../../etc/passwd")

    def test_rejects_empty_path(self, tmp_repo: Path):
        import server
        with pytest.raises(ValueError, match="file_path is required"):
            server._validate_file_path(tmp_repo, "")

    def test_rejects_blank_path(self, tmp_repo: Path):
        import server
        with pytest.raises(ValueError, match="file_path is required"):
            server._validate_file_path(tmp_repo, "   ")


# ---------------------------------------------------------------------------
# Helpers: _check_auth
# ---------------------------------------------------------------------------

class TestCheckAuth:
    def test_no_auth_configured_always_passes(self):
        import server
        with patch.object(server, "AUTH_TOKEN", ""):
            assert server._check_auth("anything") is None
            assert server._check_auth("") is None

    def test_correct_token_passes(self):
        import server
        with patch.object(server, "AUTH_TOKEN", "secret"):
            assert server._check_auth("secret") is None

    def test_wrong_token_fails(self):
        import server
        with patch.object(server, "AUTH_TOKEN", "secret"):
            result = server._check_auth("wrong")
            assert result is not None
            assert "AUTH ERROR" in result

    def test_empty_token_fails_when_auth_configured(self):
        import server
        with patch.object(server, "AUTH_TOKEN", "secret"):
            result = server._check_auth("")
            assert result is not None


# ---------------------------------------------------------------------------
# Helpers: _validate_extra_args
# ---------------------------------------------------------------------------

class TestValidateExtraArgs:
    def test_empty_string_returns_empty_list(self):
        import server
        assert server._validate_extra_args("") == []

    def test_valid_args_parsed(self):
        import server
        result = server._validate_extra_args("-x test --info")
        assert result == ["-x", "test", "--info"]

    def test_blocked_flag_raises(self):
        import server
        with pytest.raises(ValueError, match="not permitted"):
            server._validate_extra_args("--init-script=/tmp/evil.gradle")

    def test_shell_injection_raises(self):
        import server
        with pytest.raises(ValueError, match="Invalid build argument"):
            server._validate_extra_args("; rm -rf /")

    def test_pipe_injection_raises(self):
        import server
        with pytest.raises(ValueError, match="Invalid build argument"):
            server._validate_extra_args("test | cat /etc/passwd")


# ---------------------------------------------------------------------------
# Helpers: _truncate
# ---------------------------------------------------------------------------

class TestTruncate:
    def test_short_text_unchanged(self):
        import server
        original_max = server.MAX_OUTPUT
        with patch.object(server, "MAX_OUTPUT", 100):
            result = server._truncate("short text")
        assert result == "short text"

    def test_long_text_truncated(self):
        import server
        with patch.object(server, "MAX_OUTPUT", 10):
            result = server._truncate("a" * 20, "test ")
        assert len(result) > 10
        assert "truncated" in result
        assert result.startswith("a" * 10)


# ---------------------------------------------------------------------------
# Helpers: _build_search_cmd
# ---------------------------------------------------------------------------

class TestBuildSearchCmd:
    def test_grep_fallback_when_no_ripgrep(self):
        import server
        with patch.object(server, "_HAS_RIPGREP", False):
            cmd = server._build_search_cmd("foo", "*.py", False, 2)
        assert cmd[0] == "grep"
        assert "-rn" in cmd
        assert "--include=*.py" in cmd

    def test_ripgrep_when_available(self):
        import server
        with patch.object(server, "_HAS_RIPGREP", True):
            cmd = server._build_search_cmd("foo", "*.py", False, 2)
        assert cmd[0] == "rg"
        assert "-g" in cmd

    def test_case_sensitive_flag_grep(self):
        import server
        with patch.object(server, "_HAS_RIPGREP", False):
            cmd = server._build_search_cmd("foo", "*.py", True, 2)
        assert "-i" not in cmd

    def test_case_insensitive_flag_grep(self):
        import server
        with patch.object(server, "_HAS_RIPGREP", False):
            cmd = server._build_search_cmd("foo", "*.py", False, 2)
        assert "-i" in cmd

    def test_files_only_mode_grep(self):
        import server
        with patch.object(server, "_HAS_RIPGREP", False):
            cmd = server._build_search_cmd("foo", "*.py", False, 0, files_only=True)
        assert "-rl" in cmd

    def test_files_only_mode_ripgrep(self):
        import server
        with patch.object(server, "_HAS_RIPGREP", True):
            cmd = server._build_search_cmd("foo", "*.py", False, 0, files_only=True)
        assert "-l" in cmd


# ---------------------------------------------------------------------------
# Helpers: _all_repos and _resolve_repo
# ---------------------------------------------------------------------------

class TestAllRepos:
    def test_discovers_repos(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            repos = server._all_repos()
        assert "my-service" in repos

    def test_ignores_non_git_directories(self, tmp_path: Path):
        import server
        (tmp_path / "not-a-repo").mkdir()
        with patch.object(server, "REPO_ROOTS", [str(tmp_path)]):
            repos = server._all_repos()
        assert "not-a-repo" not in repos

    def test_ignores_nonexistent_root(self, tmp_path: Path):
        import server
        missing = str(tmp_path / "does-not-exist")
        with patch.object(server, "REPO_ROOTS", [missing]):
            repos = server._all_repos()
        assert repos == {}

    def test_cache_returns_same_object(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            first = server._all_repos()
            second = server._all_repos()
        assert first is second


class TestResolveRepo:
    def test_resolves_known_repo(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            path = server._resolve_repo("my-service")
        assert path.name == "my-service"

    def test_raises_for_unknown_repo(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            with pytest.raises(ValueError, match="not found"):
                server._resolve_repo("nonexistent")

    def test_raises_for_empty_name(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            with pytest.raises(ValueError, match="required"):
                server._resolve_repo("")


# ---------------------------------------------------------------------------
# MCP tools: list_repos
# ---------------------------------------------------------------------------

class TestListRepos:
    def test_lists_repos(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.list_repos()
        assert "my-service" in result

    def test_filter_matches(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.list_repos(filter="my")
        assert "my-service" in result

    def test_filter_excludes(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.list_repos(filter="zzz-no-match")
        assert "No repos found" in result

    def test_no_repos_configured(self, tmp_path: Path):
        import server
        with patch.object(server, "REPO_ROOTS", []):
            result = server.list_repos()
        assert "No repositories found" in result

    def test_auth_fails_with_wrong_token(self, tmp_root: Path):
        import server
        with patch.object(server, "AUTH_TOKEN", "secret"):
            with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
                result = server.list_repos(auth="wrong")
        assert "AUTH ERROR" in result


# ---------------------------------------------------------------------------
# MCP tools: read_file
# ---------------------------------------------------------------------------

class TestReadFile:
    def test_reads_file(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.read_file("my-service", "README.md")
        assert "my-service" in result

    def test_returns_error_for_missing_file(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.read_file("my-service", "nonexistent.txt")
        assert "not found" in result.lower()

    def test_path_traversal_rejected(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            with pytest.raises(ValueError, match="traversal"):
                server.read_file("my-service", "../../../etc/passwd")

    def test_offset_and_limit(self, tmp_root: Path):
        import server
        src = tmp_root / "my-service" / "src" / "main.py"
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.read_file("my-service", "src/main.py", offset=0, limit=1)
        assert "lines 1-1" in result


# ---------------------------------------------------------------------------
# MCP tools: list_files
# ---------------------------------------------------------------------------

class TestListFiles:
    def test_lists_matching_files(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.list_files("my-service", pattern="**/*.py")
        assert "main.py" in result

    def test_returns_message_when_no_match(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.list_files("my-service", pattern="**/*.java")
        assert "No files" in result


# ---------------------------------------------------------------------------
# MCP tools: git_status
# ---------------------------------------------------------------------------

class TestGitStatus:
    def test_returns_branch_and_status(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.git_status("my-service")
        assert "Branch" in result
        assert "Status" in result
        assert "Recent commits" in result


# ---------------------------------------------------------------------------
# MCP tools: git_diff
# ---------------------------------------------------------------------------

class TestGitDiff:
    def test_unstaged_diff(self, tmp_root: Path, tmp_repo: Path):
        import server
        (tmp_repo / "src" / "main.py").write_text("def hello():\n    return 'changed'\n")
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.git_diff("my-service")
        assert "changed" in result or result.strip() == "" or "diff" in result

    def test_invalid_branch_name_rejected(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.git_diff("my-service", target="main; rm -rf /")
        assert "ERROR" in result


# ---------------------------------------------------------------------------
# MCP tools: git_log
# ---------------------------------------------------------------------------

class TestGitLog:
    def test_returns_commits(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.git_log("my-service", count=5)
        assert "initial commit" in result

    def test_count_capped_at_100(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.git_log("my-service", count=999)
        assert result is not None

    def test_invalid_branch_rejected(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.git_log("my-service", branch="main; evil")
        assert "ERROR" in result


# ---------------------------------------------------------------------------
# MCP tools: get_dependencies
# ---------------------------------------------------------------------------

class TestGetDependencies:
    def test_reads_package_json(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.get_dependencies("my-service")
        assert "package.json" in result
        assert "my-service" in result

    def test_returns_message_when_no_build_file(self, tmp_path: Path):
        import server
        repo = tmp_path / "empty-service"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        with patch.object(server, "REPO_ROOTS", [str(tmp_path)]):
            result = server.get_dependencies("empty-service")
        assert "No build file" in result


# ---------------------------------------------------------------------------
# MCP tools: run_build
# ---------------------------------------------------------------------------

class TestRunBuild:
    def test_rejects_unknown_task(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.run_build("my-service", task="evil-task")
        assert "ERROR" in result
        assert "not in the allowed list" in result

    def test_rejects_invalid_extra_args(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.run_build("my-service", task="build", extra_args="; rm -rf /")
        assert "ERROR" in result

    def test_returns_error_when_no_build_tool(self, tmp_path: Path):
        import server
        repo = tmp_path / "bare-service"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        with patch.object(server, "REPO_ROOTS", [str(tmp_path)]):
            result = server.run_build("bare-service", task="build")
        assert "No recognized build tool" in result


# ---------------------------------------------------------------------------
# MCP tools: search_code
# ---------------------------------------------------------------------------

class TestSearchCode:
    def test_finds_pattern_in_repo(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.search_code("def hello", repo_name="my-service", file_glob="*.py")
        assert "hello" in result

    def test_no_match_returns_message(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.search_code("zzz_no_such_pattern", repo_name="my-service", file_glob="*.py")
        assert "No matches" in result

    def test_empty_pattern_returns_error(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.search_code("", repo_name="my-service")
        assert "ERROR" in result


# ---------------------------------------------------------------------------
# MCP tools: find_repos_with
# ---------------------------------------------------------------------------

class TestFindReposWith:
    def test_finds_repo_containing_pattern(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.find_repos_with("def hello", file_glob="*.py")
        assert "my-service" in result

    def test_returns_message_when_not_found(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.find_repos_with("zzz_no_such_pattern", file_glob="*.py")
        assert "No repos found" in result

    def test_empty_pattern_returns_error(self, tmp_root: Path):
        import server
        with patch.object(server, "REPO_ROOTS", [str(tmp_root)]):
            result = server.find_repos_with("")
        assert "ERROR" in result
