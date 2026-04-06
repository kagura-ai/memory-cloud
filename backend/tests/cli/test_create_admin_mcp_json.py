"""Tests for create_admin .mcp.json path resolution, best-effort writing,
and CLI flag parsing.

These cover path selection, non-raising behavior on I/O failure, and the
--skip-mcp-json argparse contract. The transaction-ordering regression for
#194 (db.commit must precede _write_mcp_json) is guarded structurally in
TestCommitOrdering via source inspection rather than a full integration
test, since exercising create_admin() end-to-end would require mocking
create_engine, Session, the interactive prompts, and several encryption
dependencies.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# create_admin uses sys.path.insert hack; import via the same path.
_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli import create_admin  # noqa: E402


class TestResolveMcpJsonPath:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_JSON_DIR", str(tmp_path))
        assert create_admin._resolve_mcp_json_path() == tmp_path / ".mcp.json"

    def test_falls_back_to_home_when_project_root_is_root(self, tmp_path, monkeypatch):
        """In Docker, _project_root resolves to '/' (no pyproject.toml)."""
        monkeypatch.delenv("MCP_JSON_DIR", raising=False)
        monkeypatch.setattr(create_admin, "_project_root", Path("/"))
        monkeypatch.setenv("HOME", str(tmp_path))
        assert create_admin._resolve_mcp_json_path() == tmp_path / ".mcp.json"

    def test_falls_back_to_cwd_when_home_unwritable(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MCP_JSON_DIR", raising=False)
        monkeypatch.setattr(create_admin, "_project_root", Path("/"))
        # HOME set but points nowhere writable
        monkeypatch.setenv("HOME", "/nonexistent-home-xyz")
        monkeypatch.chdir(tmp_path)
        assert create_admin._resolve_mcp_json_path() == tmp_path / ".mcp.json"

    def test_returns_none_when_nothing_writable(self, monkeypatch):
        monkeypatch.delenv("MCP_JSON_DIR", raising=False)
        monkeypatch.setattr(create_admin, "_project_root", Path("/"))
        monkeypatch.delenv("HOME", raising=False)
        with patch("os.access", return_value=False):
            assert create_admin._resolve_mcp_json_path() is None

    def test_host_repo_root_with_git_marker_still_works(self, tmp_path, monkeypatch):
        """On host, _project_root is the repo root — identified via .git."""
        (tmp_path / ".git").mkdir()
        monkeypatch.delenv("MCP_JSON_DIR", raising=False)
        monkeypatch.setattr(create_admin, "_project_root", tmp_path)
        assert create_admin._resolve_mcp_json_path() == tmp_path / ".mcp.json"


class TestWriteMcpJson:
    def test_writes_file_when_path_resolvable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_JSON_DIR", str(tmp_path))
        ok = create_admin._write_mcp_json("test-key", "ws-123")
        assert ok is True
        content = (tmp_path / ".mcp.json").read_text()
        assert "test-key" in content
        assert "ws-123" in content

    def test_does_not_raise_on_permission_error(self, tmp_path, monkeypatch, capsys):
        """Regression for #194: PermissionError must not propagate.

        Uses a real read-only directory to trigger a genuine OSError rather
        than patching Path.write_text globally (which would affect any other
        code running during the test).
        """
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o500)  # r-x only, no write
        monkeypatch.setenv("MCP_JSON_DIR", str(readonly))
        try:
            ok = create_admin._write_mcp_json("test-key", "ws-123")
        finally:
            readonly.chmod(0o700)  # restore so pytest can clean up

        assert ok is False  # reported as skipped, not raised
        out = capsys.readouterr().out
        assert "Permission denied" in out or "Could not write" in out
        # Config printed as fallback so operator can copy-paste
        assert "test-key" in out
        assert "ws-123" in out

    def test_does_not_raise_when_no_writable_dir(self, monkeypatch, capsys):
        monkeypatch.setattr(create_admin, "_resolve_mcp_json_path", lambda: None)
        ok = create_admin._write_mcp_json("test-key", "ws-123")
        assert ok is False
        out = capsys.readouterr().out
        assert "test-key" in out  # fallback printed


class TestArgumentParsing:
    @pytest.fixture
    def captured_skip(self, monkeypatch):
        """Replace create_admin() with a stub that records its skip_mcp_json arg."""
        captured: dict[str, bool] = {}

        def fake_create_admin(skip_mcp_json=False):
            captured["skip"] = skip_mcp_json

        monkeypatch.setattr(create_admin, "create_admin", fake_create_admin)
        return captured

    def test_skip_mcp_json_flag_parses(self, monkeypatch, captured_skip):
        monkeypatch.setattr(sys, "argv", ["create_admin", "--skip-mcp-json"])
        create_admin._main()
        assert captured_skip["skip"] is True

    def test_default_is_not_skip(self, monkeypatch, captured_skip):
        monkeypatch.setattr(sys, "argv", ["create_admin"])
        create_admin._main()
        assert captured_skip["skip"] is False


class TestCommitOrdering:
    """Structural regression guard for #194.

    The original bug: db.commit() was AFTER _write_mcp_json(), so a
    PermissionError on the file write rolled back the entire admin creation.
    Asserting ordering via source inspection is cheaper than an integration
    test that mocks create_engine + Session + interactive prompts, and it
    fails loudly if someone reverts the fix.
    """

    def test_db_commit_precedes_write_mcp_json_in_create_admin(self):
        import inspect

        source = inspect.getsource(create_admin.create_admin)
        commit_idx = source.find("db.commit()")
        write_idx = source.find("_write_mcp_json(")
        assert commit_idx != -1, "db.commit() not found in create_admin source"
        assert write_idx != -1, "_write_mcp_json( not found in create_admin source"
        assert commit_idx < write_idx, (
            "db.commit() must precede _write_mcp_json() to avoid rollback on "
            "file-write failure — see #194."
        )
