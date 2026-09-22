"""The documented Codex cloud recipe (docs/mcp-clients.md § Codex cloud) does what it says (#1621).

The recipe is not shipped as a script — a plugin script never reaches a cloud
container — so the fenced block in the docs IS the artifact. These tests
extract it (anchored on its first comment line), run the bash half with a stub
``curl`` on ``PATH`` and the Python half directly, and pin every property the
docs claim: idempotent, text outside the markers untouched, append when the
markers are missing, regex-replacement escapes written literally, a summary
containing the end-marker text cannot break the block, a malformed fetched
block is refused, an empty digest removes an earlier block and never creates
one, an unset ``KAGURA_API_KEY`` / ``KAGURA_CONTEXT_ID`` or a path-bearing
``KAGURA_API_BASE`` is refused before any fetch, every failure is exactly one
stderr line + ``exit 0`` with the file unchanged, and in a git repository the
write leaves ``git status`` clean — skip-worktree for a tracked ``AGENTS.md``,
``.git/info/exclude`` for one the recipe created.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DOC = Path(__file__).resolve().parents[3] / "docs" / "mcp-clients.md"
ANCHOR = "# Kagura Memory guardrails → AGENTS.md"

CTX = "550e8400-e29b-41d4-a716-446655440000"
BEGIN = (
    f"<!-- kagura-memory:guardrails begin context={CTX} tool_triggered_version=0123456789abcdef -->"
)
END = "<!-- kagura-memory:guardrails end -->"


def _block(*lines: str) -> str:
    return "\n".join([BEGIN, *lines, END]) + "\n"


BLOCK = _block("- (3f9c1a7b) never force-push a shared branch", "- (a1b2c3d4) squash merge only")


def _recipe() -> str:
    text = DOC.read_text(encoding="utf-8")
    fenced = re.findall(r"```bash\n(.*?)```", text, flags=re.S)
    matches = [body for body in fenced if body.startswith(ANCHOR)]
    assert len(matches) == 1, "exactly one recipe block anchored on the comment line"
    return matches[0]


def _python_half(recipe: str) -> str:
    match = re.search(r"python3 - <<'PY'.*?\n(.*?)\nPY\n", recipe, flags=re.S)
    assert match, "the recipe carries one python3 heredoc"
    return match.group(1)


@pytest.fixture(scope="module")
def recipe() -> str:
    return _recipe()


@pytest.fixture(scope="module")
def python_half(recipe: str) -> str:
    return _python_half(recipe)


def run_python(python_half: str, path: Path, block: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "KAGURA_BLOCK": block, "KAGURA_AGENTS_MD": str(path)}
    return subprocess.run(
        [sys.executable, "-"], input=python_half, text=True, env=env, capture_output=True
    )


# --------------------------------------------------------------- python half


def test_docs_carry_exactly_one_recipe_with_the_documented_guards(recipe):
    assert "set -u" in recipe
    assert "--skip-worktree AGENTS.md" in recipe
    assert "info/exclude" in recipe  # an untracked AGENTS.md is excluded locally
    assert "git rev-parse --show-toplevel" in recipe
    assert "https://?*)" in recipe and "https://*/*)" in recipe  # scheme + host, no path
    assert '*"/mcp"*)' in recipe
    assert '"${KAGURA_API_KEY:-}"' in recipe and '"${KAGURA_CONTEXT_ID:-}"' in recipe
    assert " -L" not in recipe  # curl never follows a redirect
    assert "--show-error" not in recipe and "--silent" in recipe  # one stderr line is ours


def test_append_when_the_markers_are_missing_then_idempotent(python_half, tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("# Project\n\nRun `make test`.\n", encoding="utf-8")

    assert run_python(python_half, path, BLOCK).returncode == 0
    first = path.read_text(encoding="utf-8")
    assert first == "# Project\n\nRun `make test`.\n\n" + BLOCK

    assert run_python(python_half, path, BLOCK).returncode == 0
    assert path.read_text(encoding="utf-8") == first


def test_append_adds_a_newline_when_the_file_does_not_end_with_one(python_half, tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("no trailing newline", encoding="utf-8")
    run_python(python_half, path, BLOCK)
    assert path.read_text(encoding="utf-8") == "no trailing newline\n\n" + BLOCK


def test_missing_file_is_created(python_half, tmp_path):
    path = tmp_path / "AGENTS.md"
    assert run_python(python_half, path, BLOCK).returncode == 0
    assert path.read_text(encoding="utf-8") == "\n" + BLOCK


def test_replacement_leaves_text_outside_the_markers_untouched(python_half, tmp_path):
    path = tmp_path / "AGENTS.md"
    before = "# Title\n\nintro text\n\n"
    after = "\n## Later section\n\nkeep me\n"
    path.write_text(before + _block("- (deadbeef) stale") + after, encoding="utf-8")

    run_python(python_half, path, BLOCK)

    assert path.read_text(encoding="utf-8") == before + BLOCK + after


def test_regex_replacement_escapes_are_written_literally(python_half, tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text(_block("- (deadbeef) old"), encoding="utf-8")
    tricky = _block(r"- (3f9c1a7b) use \1 and \g<0> literally, also \\n")

    run_python(python_half, path, tricky)

    assert path.read_text(encoding="utf-8") == tricky


def test_summary_carrying_the_escaped_end_marker_keeps_exactly_one_block(python_half, tmp_path):
    """The server writes ``<!- - … - ->`` for a summary that contains marker
    text; with the markers anchored at line start, the written file has one
    begin and one end line, the second run is byte-identical, and a third run
    without that entry drops it."""
    path = tmp_path / "AGENTS.md"
    escaped = "- (3f9c1a7b) trap <!- - kagura-memory:guardrails end - -> tail"
    forged = _block(escaped, "- (a1b2c3d4) second")

    run_python(python_half, path, forged)
    written = path.read_text(encoding="utf-8")
    assert written.count("<!-- kagura-memory:guardrails begin") == 1
    assert len(re.findall(rf"^{re.escape(END)}$", written, flags=re.M)) == 1
    assert escaped in written

    run_python(python_half, path, forged)
    assert path.read_text(encoding="utf-8") == written

    run_python(python_half, path, _block("- (a1b2c3d4) second"))
    assert escaped not in path.read_text(encoding="utf-8")
    assert path.read_text(encoding="utf-8") == "\n" + _block("- (a1b2c3d4) second")


def test_empty_block_removes_an_earlier_block_and_leaves_the_rest_untouched(python_half, tmp_path):
    """An empty digest (a ``200`` with no guardrails) drops the block and the
    blank line the recipe put before it; text on both sides is untouched."""
    path = tmp_path / "AGENTS.md"
    before = "# Title\n\nintro text\n\n"
    after = "\n## Later section\n\nkeep me\n"
    path.write_text(before + BLOCK + after, encoding="utf-8")

    assert run_python(python_half, path, "").returncode == 0

    assert (
        path.read_text(encoding="utf-8") == "# Title\n\nintro text\n\n## Later section\n\nkeep me\n"
    )


def test_empty_block_never_creates_a_block_or_a_file(python_half, tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("# Project\n", encoding="utf-8")
    assert run_python(python_half, path, "").returncode == 0
    assert path.read_text(encoding="utf-8") == "# Project\n"

    missing = tmp_path / "none.md"
    assert run_python(python_half, missing, "\n").returncode == 0
    assert not missing.exists()


def test_append_then_empty_block_round_trips_to_the_original_bytes(python_half, tmp_path):
    path = tmp_path / "AGENTS.md"
    original = "# Project\n\nRun `make test`.\n"
    path.write_text(original, encoding="utf-8")

    run_python(python_half, path, BLOCK)
    run_python(python_half, path, "")
    assert path.read_text(encoding="utf-8") == original

    created = tmp_path / "created.md"  # a file the recipe made holds only the block
    run_python(python_half, created, BLOCK)
    assert created.read_text(encoding="utf-8") == "\n" + BLOCK
    run_python(python_half, created, "")
    assert created.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    "bad",
    [
        BEGIN + "\n" + BEGIN + "\n- (x) two begins\n" + END + "\n",
        BEGIN + "\n- (x) two ends\n" + END + "\n" + END + "\n",
        "- (x) no markers at all\n",
        BEGIN + "\n- (x) no end\n",
    ],
)
def test_malformed_fetched_block_is_refused_and_the_file_is_unchanged(python_half, tmp_path, bad):
    path = tmp_path / "AGENTS.md"
    original = "# Title\n\n" + BLOCK
    path.write_text(original, encoding="utf-8")

    result = run_python(python_half, path, bad)

    assert result.returncode != 0  # the bash half turns this into one stderr line + exit 0
    assert "exactly one begin and one end marker line" in result.stderr
    assert path.read_text(encoding="utf-8") == original


def test_a_file_with_two_blocks_is_left_for_a_human(python_half, tmp_path):
    path = tmp_path / "AGENTS.md"
    original = BLOCK + "\n" + BLOCK
    path.write_text(original, encoding="utf-8")

    result = run_python(python_half, path, BLOCK)

    assert result.returncode != 0
    assert "more than one guardrail block" in result.stderr
    assert path.read_text(encoding="utf-8") == original


# ----------------------------------------------------------------- bash half


def _stub_bin(tmp_path: Path, *, body: str | None = None, exit_code: int = 0) -> Path:
    """A ``PATH`` directory with a stub ``curl`` that records its arguments and
    prints ``body`` (or exits ``exit_code``) plus links to the real tools."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" > "$KAGURA_TEST_CURL_ARGS"\n'
        f"[ {exit_code} -eq 0 ] || exit {exit_code}\n"
        'cat "$KAGURA_TEST_CURL_BODY"\n',
        encoding="utf-8",
    )
    (bin_dir / "curl").chmod(0o755)
    (tmp_path / "curl_body").write_text(body or "", encoding="utf-8")
    for tool in ("bash", "git", "python3", "cat", "printf"):
        real = shutil.which(tool)
        if real:
            (bin_dir / tool).symlink_to(real)
    return bin_dir


def run_bash(
    recipe: str,
    cwd: Path,
    bin_dir: Path,
    *,
    env: dict[str, str] | None = None,
    without: tuple[str, ...] = (),
    unset: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    for tool in without:
        (bin_dir / tool).unlink(missing_ok=True)
    full_env = {
        "PATH": str(bin_dir),
        "HOME": str(cwd),
        "KAGURA_API_KEY": "kagura_test_key",
        "KAGURA_API_BASE": "https://example.test",
        "KAGURA_CONTEXT_ID": CTX,
        "KAGURA_TEST_CURL_ARGS": str(bin_dir.parent / "curl_args"),
        "KAGURA_TEST_CURL_BODY": str(bin_dir.parent / "curl_body"),
        **(env or {}),
    }
    for name in unset:
        full_env.pop(name, None)
    return subprocess.run(
        ["bash", "-c", recipe], cwd=cwd, env=full_env, text=True, capture_output=True
    )


def _one_line(stderr: str) -> bool:
    return stderr.startswith("kagura guardrails: ") and stderr.count("\n") == 1


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


def test_bash_half_writes_the_block_and_calls_the_rest_endpoint_without_l(
    recipe, tmp_path, workdir
):
    bin_dir = _stub_bin(tmp_path, body=BLOCK)
    (workdir / "AGENTS.md").write_text("# Project\n", encoding="utf-8")

    result = run_bash(recipe, workdir, bin_dir)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert (workdir / "AGENTS.md").read_text(encoding="utf-8") == "# Project\n\n" + BLOCK
    args = (tmp_path / "curl_args").read_text(encoding="utf-8").split("\n")
    assert f"https://example.test/api/v1/memory/guardrails/digest?context_id={CTX}" in args
    assert "Authorization: Bearer kagura_test_key" in args
    assert "-L" not in args and "--location" not in args


def test_bash_half_non_repo_directory_writes_without_git_noise(recipe, tmp_path, workdir):
    bin_dir = _stub_bin(tmp_path, body=BLOCK)

    result = run_bash(recipe, workdir, bin_dir)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""  # the skip-worktree step is a silent no-op
    assert (workdir / "AGENTS.md").read_text(encoding="utf-8") == "\n" + BLOCK


def test_bash_half_in_a_git_repo_leaves_the_working_tree_clean(recipe, tmp_path, workdir):
    bin_dir = _stub_bin(tmp_path, body=BLOCK)
    git_env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.test",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    env = {**os.environ, **git_env}
    subprocess.run(["git", "init", "-q", str(workdir)], check=True, env=env)
    nested = workdir / "sub"
    nested.mkdir()
    (workdir / "AGENTS.md").write_text("# Project\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workdir), "add", "AGENTS.md"], check=True, env=env)
    subprocess.run(["git", "-C", str(workdir), "commit", "-q", "-m", "init"], check=True, env=env)

    # Run from a sub-directory: the path resolves from the repo root.
    result = run_bash(recipe, nested, bin_dir, env=git_env)

    assert result.returncode == 0, result.stderr
    assert (workdir / "AGENTS.md").read_text(encoding="utf-8") == "# Project\n\n" + BLOCK
    status = subprocess.run(
        ["git", "-C", str(workdir), "status", "--porcelain"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert status.stdout == ""
    assert subprocess.run(["git", "-C", str(workdir), "diff", "--quiet"], env=env).returncode == 0


def test_bash_half_curl_failure_leaves_the_file_unchanged(recipe, tmp_path, workdir):
    bin_dir = _stub_bin(tmp_path, exit_code=22)
    (workdir / "AGENTS.md").write_text("# Project\n", encoding="utf-8")

    result = run_bash(recipe, workdir, bin_dir)

    assert result.returncode == 0
    assert "kagura guardrails: fetch failed (curl exit 22)" in result.stderr
    assert _one_line(result.stderr)
    assert (workdir / "AGENTS.md").read_text(encoding="utf-8") == "# Project\n"


def test_bash_half_empty_body_removes_an_earlier_block_and_writes_nothing_new(
    recipe, tmp_path, workdir
):
    """A guardrail set that emptied on the server must not survive in the
    next task: the empty ``200`` removes the block written earlier. With no
    block present the file is byte-identical and none is created."""
    bin_dir = _stub_bin(tmp_path, body="")
    (workdir / "AGENTS.md").write_text("# Project\n\n" + BLOCK, encoding="utf-8")

    result = run_bash(recipe, workdir, bin_dir)

    assert result.returncode == 0
    assert "no guardrails in context" in result.stderr and _one_line(result.stderr)
    assert (workdir / "AGENTS.md").read_text(encoding="utf-8") == "# Project\n"

    result = run_bash(recipe, workdir, bin_dir)
    assert result.returncode == 0
    assert (workdir / "AGENTS.md").read_text(encoding="utf-8") == "# Project\n"

    (workdir / "AGENTS.md").unlink()
    result = run_bash(recipe, workdir, bin_dir)
    assert result.returncode == 0
    assert not (workdir / "AGENTS.md").exists()


def test_bash_half_refuses_a_block_with_two_begin_lines(recipe, tmp_path, workdir):
    bin_dir = _stub_bin(tmp_path, body=BEGIN + "\n" + BEGIN + "\n- (x) y\n" + END + "\n")
    (workdir / "AGENTS.md").write_text("# Project\n", encoding="utf-8")

    result = run_bash(recipe, workdir, bin_dir)

    assert result.returncode == 0
    assert result.stderr == (
        "kagura guardrails: write failed: fetched block does not have exactly one begin "
        "and one end marker line; AGENTS.md unchanged\n"
    )
    assert (workdir / "AGENTS.md").read_text(encoding="utf-8") == "# Project\n"


def test_bash_half_without_python3_is_one_stderr_line_and_exit_0(recipe, tmp_path, workdir):
    bin_dir = _stub_bin(tmp_path, body=BLOCK)
    (workdir / "AGENTS.md").write_text("# Project\n", encoding="utf-8")

    result = run_bash(recipe, workdir, bin_dir, without=("python3",))

    assert result.returncode == 0
    assert result.stderr.strip() == "kagura guardrails: python3 not found; AGENTS.md unchanged"
    assert (workdir / "AGENTS.md").read_text(encoding="utf-8") == "# Project\n"
    assert not (tmp_path / "curl_args").exists()  # refused before any fetch


@pytest.mark.parametrize(
    ("base", "message"),
    [
        ("http://example.test", "must be https://<host>"),
        ("https://example.test/mcp/w/ws", "must not contain /mcp"),
        ("https://example.test/api/v1", "scheme and host only, no path"),
        ("https://", "must be https://<host>"),
        ("", "must be https://<host>"),
    ],
)
def test_bash_half_rejects_a_bad_api_base_before_any_fetch(
    recipe, tmp_path, workdir, base, message
):
    bin_dir = _stub_bin(tmp_path, body=BLOCK)
    (workdir / "AGENTS.md").write_text("# Project\n", encoding="utf-8")

    result = run_bash(recipe, workdir, bin_dir, env={"KAGURA_API_BASE": base})

    assert result.returncode == 0
    assert _one_line(result.stderr)
    assert message in result.stderr
    assert (workdir / "AGENTS.md").read_text(encoding="utf-8") == "# Project\n"
    assert not (tmp_path / "curl_args").exists()


def test_bash_half_accepts_a_trailing_slash_on_the_api_base(recipe, tmp_path, workdir):
    bin_dir = _stub_bin(tmp_path, body=BLOCK)

    result = run_bash(recipe, workdir, bin_dir, env={"KAGURA_API_BASE": "https://example.test/"})

    assert result.returncode == 0 and result.stderr == ""
    args = (tmp_path / "curl_args").read_text(encoding="utf-8").split("\n")
    assert f"https://example.test/api/v1/memory/guardrails/digest?context_id={CTX}" in args


@pytest.mark.parametrize("name", ["KAGURA_API_KEY", "KAGURA_CONTEXT_ID"])
def test_bash_half_unset_variable_is_one_stderr_line_and_exit_0_not_a_set_u_abort(
    recipe, tmp_path, workdir, name
):
    """``set -u`` would abort on ``${KAGURA_API_KEY}`` before any ``||``
    handler ran — a bash error line and exit 1, i.e. a failed setup. The
    recipe checks both variables with ``${…:-}`` first."""
    bin_dir = _stub_bin(tmp_path, body=BLOCK)
    (workdir / "AGENTS.md").write_text("# Project\n", encoding="utf-8")

    result = run_bash(recipe, workdir, bin_dir, unset=(name,))

    assert result.returncode == 0
    assert _one_line(result.stderr) and name in result.stderr
    assert "unbound variable" not in result.stderr
    assert (workdir / "AGENTS.md").read_text(encoding="utf-8") == "# Project\n"
    assert not (tmp_path / "curl_args").exists()


def test_bash_half_rejects_a_context_id_that_is_not_uuid_shaped(recipe, tmp_path, workdir):
    bin_dir = _stub_bin(tmp_path, body=BLOCK)

    result = run_bash(recipe, workdir, bin_dir, env={"KAGURA_CONTEXT_ID": "abc&target=x"})

    assert result.returncode == 0
    assert _one_line(result.stderr) and "KAGURA_CONTEXT_ID" in result.stderr
    assert not (tmp_path / "curl_args").exists()


def test_bash_half_untracked_agents_md_is_excluded_locally(recipe, tmp_path, workdir):
    """A repository without an ``AGENTS.md``: the recipe creates one, and
    ``skip-worktree`` cannot apply to an untracked file, so it goes into
    ``.git/info/exclude`` — ``git status`` stays clean and nothing is committed."""
    bin_dir = _stub_bin(tmp_path, body=BLOCK)
    git_env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.test",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    env = {**os.environ, **git_env}
    subprocess.run(["git", "init", "-q", str(workdir)], check=True, env=env)
    (workdir / "README.md").write_text("# Project\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workdir), "add", "README.md"], check=True, env=env)
    subprocess.run(["git", "-C", str(workdir), "commit", "-q", "-m", "init"], check=True, env=env)

    result = run_bash(recipe, workdir, bin_dir, env=git_env)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert (workdir / "AGENTS.md").read_text(encoding="utf-8") == "\n" + BLOCK
    status = subprocess.run(
        ["git", "-C", str(workdir), "status", "--porcelain"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert status.stdout == ""
    ignored = subprocess.run(
        ["git", "-C", str(workdir), "check-ignore", "-q", "AGENTS.md"], env=env
    )
    assert ignored.returncode == 0
    assert (workdir / ".git" / "info" / "exclude").read_text(encoding="utf-8").count(
        "AGENTS.md"
    ) == 1

    run_bash(recipe, workdir, bin_dir, env=git_env)  # second run adds no second exclude line
    assert (workdir / ".git" / "info" / "exclude").read_text(encoding="utf-8").count(
        "AGENTS.md"
    ) == 1
