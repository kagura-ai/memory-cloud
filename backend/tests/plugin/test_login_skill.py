"""``/kagura-memory:login`` and the Codex skill's "Login" section (#1704).

Pins the Claude Code command (registration through the plugin manifest, the four steps,
the secret rules), its Codex counterpart in the kagura-memory ``SKILL.md``, the parity
between the two, the detection it shares verbatim with ``/kagura-memory:setup``, and the
docs that point to it. The scope facts are checked against the server's own tables, so a
change to the OAuth scopes fails here instead of leaving the skills stale.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from auth.mcp_scopes import ALL_ADVERTISED_SCOPES
from mcp_server.tools._scopes import (
    READ_SCOPE,
    WRITE_SCOPE,
    challenge_scope,
    required_scope_for_tool,
)
from tests.plugin.conftest import CLAUDE_PLUGIN_JSON, REPO_ROOT

COMMANDS_DIR = REPO_ROOT / "claude-skills"
LOGIN_SKILL = COMMANDS_DIR / "login.md"
SETUP_SKILL = COMMANDS_DIR / "setup.md"
CODEX_SKILL = REPO_ROOT / "plugins" / "kagura-memory" / "skills" / "kagura-memory" / "SKILL.md"
GUIDE = COMMANDS_DIR / "guide.md"
CLIENT_DOCS = REPO_ROOT / "docs" / "mcp-clients.md"
MCP_TOOLS_DOCS = REPO_ROOT / "docs" / "mcp-tools.md"
README_JA = REPO_ROOT / "README.ja.md"
TROUBLESHOOTING = REPO_ROOT / "docs" / "troubleshooting.md"
DEV_GUIDE = REPO_ROOT / "CLAUDE.md"
CLAUDE_MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"

# The versions the commands were checked against (help output / source), named in the text.
CODEX_CLI_VERSION = "codex-cli 0.145.0"
CLAUDE_CODE_VERSION = "Claude Code 2.1.283"
PYTHON_SDK_VERSION = "`kagura-memory` 0.41.3"

CLI_PROFILE_LOGIN = "kagura auth login --profile <name> --server https://<host>"

# Every file that talks about signing the Kagura connection in.
LOGIN_DOCS = [
    LOGIN_SKILL,
    CODEX_SKILL,
    GUIDE,
    CLIENT_DOCS,
    README_JA,
    SETUP_SKILL,
    TROUBLESHOOTING,
    DEV_GUIDE,
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Whitespace-normalised text, so a phrase wrapped across lines still matches."""
    return " ".join(text.split())


def _section(text: str, heading: str) -> str:
    """From ``heading`` (a whole line) up to the next heading of the same or a higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines(keepends=True)
    start = lines.index(heading + "\n")
    fenced = False
    for end in range(start + 1, len(lines)):
        line = lines[end]
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(rf"#{{1,{level}}} ", line):
            return "".join(lines[start:end])
    return "".join(lines[start:])


def _login() -> str:
    return _read(LOGIN_SKILL)


def _codex_login() -> str:
    return _section(_read(CODEX_SKILL), "## Login")


def _fenced_blocks(text: str) -> list[str]:
    return re.findall(r"```[a-z]*\n(.*?)```", text, flags=re.DOTALL)


def _block_containing(text: str, needle: str) -> str:
    blocks = [b for b in _fenced_blocks(text) if needle in b]
    assert len(blocks) == 1, f"expected one code block containing {needle!r}, got {len(blocks)}"
    return blocks[0]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_login_ships_as_a_plugin_command() -> None:
    """Commands are registered by directory: every ``claude-skills/*.md`` is one (#1704)."""
    manifest = json.loads(_read(CLAUDE_PLUGIN_JSON))
    assert manifest["commands"] == ["./claude-skills/"]
    marketplace = json.loads(_read(CLAUDE_MARKETPLACE))
    entry = next(p for p in marketplace["plugins"] if p["name"] == manifest["name"])
    plugin_root = (CLAUDE_MARKETPLACE.parent.parent / entry["source"]).resolve()
    commands = (plugin_root / manifest["commands"][0]).resolve()
    assert commands == COMMANDS_DIR.resolve()
    assert (commands / "login.md").is_file()
    assert f"/{manifest['name']}:{LOGIN_SKILL.stem}" == "/kagura-memory:login"
    text = _login()
    assert text.startswith("---\ndescription: "), "front matter must match the sibling commands"
    front = text.split("---", 2)[1].strip()
    assert front.startswith("description:") and "\n" not in front, "one front-matter key only"
    assert "insufficient_scope" in front


def _listed(path: Path, pattern: str) -> set[str]:
    return set(re.findall(pattern, _read(path), flags=re.MULTILINE))


def test_every_command_is_listed_where_the_commands_are_listed() -> None:
    commands = {path.stem for path in COMMANDS_DIR.glob("*.md")}
    assert "login" in commands
    assert _listed(GUIDE, r"^\| `([a-z-]+)` \|") == commands
    assert _listed(CLIENT_DOCS, r"^\| `/kagura-memory:([a-z-]+)` \|") == commands
    assert _listed(README_JA, r"^\| `/kagura-memory:([a-z-]+)` \|") == commands
    # The development guide lists them on one line, under the plugin's directory.
    (dev_line,) = _listed(DEV_GUIDE, r"^  - Commands: (.*)$")
    assert set(re.findall(r"`/kagura-memory:([a-z-]+)`", dev_line)) == commands


def test_codex_skill_maps_the_login_command_and_triggers_on_it() -> None:
    text = _read(CODEX_SKILL)
    assert re.search(r"^- `/kagura-memory:login` -> ", text, flags=re.MULTILINE)
    description = text.split("---", 2)[1]
    assert "insufficient_scope" in description and "invalid_token" in description


# ---------------------------------------------------------------------------
# The flow: detect -> re-authenticate -> insufficient_scope -> verify
# ---------------------------------------------------------------------------

CLAUDE_STEPS = [
    "## 1. Detect",
    "## 2. Re-authenticate",
    "## 3. After `insufficient_scope`",
    "## 4. Verify",
]
CODEX_STEPS = [
    "1. **Detect.**",
    "2. **Re-authenticate.**",
    "3. **After `insufficient_scope`.**",
    "4. **Verify.**",
]


@pytest.mark.parametrize(
    ("text", "steps"),
    [(_login, CLAUDE_STEPS), (_codex_login, CODEX_STEPS)],
    ids=["claude", "codex"],
)
def test_both_skills_run_the_four_steps_in_order(text, steps: list[str]) -> None:
    body = text()
    positions = [body.index(step) for step in steps]
    assert positions == sorted(positions), steps


def test_login_skill_reauthenticates_every_entry_form() -> None:
    step2 = _section(_login(), "## 2. Re-authenticate")
    for form in ("### OAuth (Claude Code)", "### CLI profile (`kagura-mcp`)", "### Bearer key"):
        assert form in step2, form
    flat = _flat(step2)
    # OAuth: Claude Code's own flow, which the skill can only instruct.
    assert "Run `/mcp`, choose **kagura-memory**, then **Authenticate**" in flat
    assert "claude mcp login kagura-memory" in flat and CLAUDE_CODE_VERSION in flat
    assert "claude mcp logout kagura-memory" in flat
    # CLI profile: the SDK's device flow, in the user's own terminal.
    assert CLI_PROFILE_LOGIN in step2 and PYTHON_SDK_VERSION in flat
    assert "**site root**" in flat and "/mcp/mcp" in flat
    # Bearer key: replaced where the entry reads it, never pasted into the conversation.
    assert "Workspace → Integrations → API Keys" in flat
    assert "never pasted here" in flat
    assert "`api_key`" in flat, "the hooks' key is a separate credential"


@pytest.mark.parametrize(
    "text",
    [lambda: _section(_login(), "### CLI profile (`kagura-mcp`)"), _codex_login],
    ids=["claude", "codex"],
)
def test_cli_profile_login_names_the_profile_the_proxy_reads(text) -> None:
    """``kagura-mcp`` without ``--profile`` reads the credentials file's ``default_profile``;
    ``kagura auth login`` without ``--profile`` writes one literally named ``default``
    (Python SDK 0.41.3: ``mcp_proxy.py`` ``--profile`` default ``None``, ``auth/cli.py``
    ``--profile`` default ``"default"``). Logging in to ``default`` then leaves the proxy on
    its dead profile, so the name must come from the row ``kagura auth list`` marks default.
    """
    flat = _flat(text())
    assert "marks `default`" in flat
    assert "`default` when `Args` name none" not in flat


def test_codex_detects_the_cli_profile_by_the_path_setup_writes() -> None:
    """``kagura setup codex`` names ``kagura-mcp`` by absolute path (SDK ``setup_harness.py``)."""
    flat = _flat(_codex_login())
    assert "a path ending in `kagura-mcp`" in flat
    assert "absolute path" in flat


def test_codex_explains_unsupported_on_a_url_only_entry() -> None:
    """A ``url``-only entry whose server gives Codex no OAuth metadata shows ``Unsupported``."""
    flat = _flat(_codex_login())
    assert "`Unsupported` on an entry with only a `url`" in flat
    assert "rust-v0.155.1" in flat, "the two Codex versions in the file are told apart"


def test_bearer_fallback_keeps_the_key_out_of_shell_history() -> None:
    bearer = _flat(_section(_login(), "### Bearer key"))
    assert "Bearer <new key>" not in bearer, "a key typed on the command line lands in history"
    assert "`read -rs KAGURA_NEW_KEY`" in bearer
    assert '--header "Authorization: Bearer $KAGURA_NEW_KEY"' in bearer
    assert "`unset KAGURA_NEW_KEY`" in bearer
    assert "shell history" in bearer


def test_login_skill_makes_and_removes_the_values_directory() -> None:
    detect = _flat(_section(_login(), "## 1. Detect"))
    assert "`entry_name`" in detect
    assert "`mktemp -d`" in detect
    assert '`rm -rf "<values dir>"`' in detect


@pytest.mark.parametrize("text", [_login, _codex_login], ids=["claude", "codex"])
def test_both_skills_request_the_challenge_scope_after_insufficient_scope(text) -> None:
    body = _flat(text())
    assert "exactly the challenge's `scope`" in body
    assert "the missing scope alone" in body
    assert "`required_scope`" in body
    assert f"{READ_SCOPE} {WRITE_SCOPE}" in body
    assert "docs/mcp-tools.md#oauth-scopes" in body
    # Only names the server advertises may go into a command.
    for scope in ALL_ADVERTISED_SCOPES:
        assert f"`{scope}`" in body, scope


def test_scope_facts_match_the_server() -> None:
    """The skills' claims about scopes are the server's rules (#1686)."""
    assert (READ_SCOPE, WRITE_SCOPE) == ("memory:read", "memory:write")
    # The verify call passes on a read-only token, so it proves the connection only.
    assert required_scope_for_tool("list_contexts") == READ_SCOPE
    assert required_scope_for_tool("remember") == WRITE_SCOPE
    assert "\n## OAuth scopes\n" in _read(MCP_TOOLS_DOCS)


def test_login_skill_scope_step_covers_both_oauth_forms() -> None:
    step3 = _flat(_section(_login(), "## 3. After `insufficient_scope`"))
    assert 'WWW-Authenticate: Bearer error="insufficient_scope", scope="…"' in step3
    assert "claude mcp logout kagura-memory" in step3
    assert CLI_PROFILE_LOGIN + ' --scope "<scope>"' in step3
    assert "`--read-only`" in step3


@pytest.mark.parametrize("text", [_login, _codex_login], ids=["claude", "codex"])
def test_both_skills_verify_with_one_read_call(text) -> None:
    body = _flat(text())
    assert "list_contexts()" in body
    assert "a read-only token passes it too" in body
    assert "ask first" in body, "retrying the refused write is the user's call"
    assert "workspace" in body.lower()


def test_login_skill_verify_step_reports_and_never_falls_back() -> None:
    step4 = _section(_login(), "## 4. Verify")
    assert _block_containing(step4, "list_contexts()").strip() == "list_contexts()"
    flat = _flat(step4)
    assert "MCP not verified — the Kagura tools are not loaded in this session" in flat
    assert "Never fall back to calling the server with a credential" in flat
    assert "Do not loop" in flat


# ---------------------------------------------------------------------------
# Shared with /kagura-memory:setup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "needle", ["claude mcp get kagura-memory | sed", "kagura auth list --json"]
)
def test_login_skill_reuses_setup_detection_verbatim(needle: str) -> None:
    """The two read-only detection blocks are setup's B1, byte for byte."""
    assert _block_containing(_login(), needle) == _block_containing(_read(SETUP_SKILL), needle)


def test_login_skill_points_at_setup_for_everything_else() -> None:
    setup = _read(SETUP_SKILL)
    for heading in ("## Rules", "### A1.", "### B0.", "### B1.", "### B3."):
        assert heading in setup, heading
    flat = _flat(_login())
    assert "`/kagura-memory:setup`'s B1" in flat and '"Entry forms"' in flat
    assert "B0 value check" in flat
    assert "(B3)" in flat and "A1" in flat
    b1 = setup.split("### B1.", 1)[1].split("### B2.")[0]
    assert "<!-- SYNC:" in b1 and "claude-skills/login.md" in b1


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------

LOGIN_TEXTS = [_login, _codex_login]


@pytest.mark.parametrize("text", LOGIN_TEXTS, ids=["claude", "codex"])
def test_login_text_never_asks_for_a_secret(text) -> None:
    body = text()
    flat = _flat(body)
    # Nothing key-shaped, and no re-implemented device flow.
    assert not re.search(r"kagura_[A-Za-z0-9]", body)
    for reimplemented in ("device_code", "grant_type", "/oauth/"):
        assert reimplemented not in body
    # Every mention of pasting is a prohibition.
    for sentence in re.split(r"(?<=[.;])\s+", flat):
        if re.search(r"\bpaste", sentence, flags=re.IGNORECASE):
            assert re.search(r"\b(never|not)\b", sentence), sentence
    for ask in ("paste your", "paste the key", "paste the token", "send me", "share your"):
        assert ask not in flat.lower(), ask
    # Sign-ins wait for a browser: the user's own terminal, never `!` (its output joins the
    # conversation).
    assert "own terminal" in flat and "`!`" in flat


def test_login_skill_states_the_credential_rules() -> None:
    rules = _flat(_section(_login(), "## Rules"))
    assert (
        "Never print, ask for or store a token, an API key, a device-flow code or an OAuth "
        "redirect URL" in rules
    )
    assert "never with `!`" in rules
    assert "Never run `claude mcp get` unfiltered" in rules
    assert "`kagura auth token`" in rules and "`~/.kagura/credentials.json`" in rules
    assert "Change no MCP entry's URL here" in rules
    assert "never command text" in rules


def test_login_skill_runs_only_fixed_read_only_commands() -> None:
    """What the skill runs itself: no secret printer, no unfiltered get, no spliced value."""
    runs = ("claude mcp list", "claude mcp get", "kagura auth list")
    for block in _fenced_blocks(_login()):
        for line in block.splitlines():
            stripped = line.strip()
            assert not stripped.startswith(("kagura auth token", "kagura auth status")), line
            if stripped.startswith("claude mcp get"):
                assert "| sed -E" in stripped, "never run claude mcp get unfiltered"
            if stripped.startswith(runs):
                assert set(re.findall(r"<[^>]+>", stripped)) <= {"<redacted>"}, line


def test_codex_login_never_reads_config_as_json() -> None:
    """``codex mcp list/get --json`` prints ``http_headers`` and ``env`` values in clear."""
    section = _codex_login()
    flat = _flat(section)
    assert "Never add `--json`" in flat
    assert "`*****`" in flat, "the plain forms mask secret values"
    assert not re.search(r"codex mcp (list|get)[^`\n]*--json", section)


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def test_codex_login_section_names_its_commands_and_version() -> None:
    text = _read(CODEX_SKILL)
    section = _codex_login()
    flat = _flat(section)
    sync = text[: text.index("\n## Login\n")].rstrip().splitlines()[-1]
    assert sync.startswith("<!-- SYNC:") and "claude-skills/login.md" in sync
    assert CODEX_CLI_VERSION in flat
    assert "codex mcp list" in flat and "`Not logged in`" in flat
    assert "codex mcp login kagura-memory" in flat
    assert "codex mcp login kagura-memory --scopes <scopes>" in flat
    assert "codex mcp logout kagura-memory" in flat
    assert CLI_PROFILE_LOGIN in flat
    # Where OAuth is not available: the Bearer-key path of the existing setup section.
    assert '"Tool Availability"' in flat and "\n## Tool Availability\n" in text
    assert "```toml" not in section, "test_codex_config_snippets scans every toml fence"


def test_codex_scope_commands_carry_the_whole_challenge() -> None:
    """The challenge can hold ``openid`` / ``offline_access`` / ``memory:delete`` too
    (``challenge_scope`` keeps every advertised scope the token has), so a fixed
    ``memory:read,memory:write`` would drop them (PR #1712 review).
    """
    assert challenge_scope(frozenset({READ_SCOPE, "offline_access"}), WRITE_SCOPE) == (
        f"{READ_SCOPE} {WRITE_SCOPE} offline_access"
    )
    step3 = _flat(_codex_login().split("3. **After `insufficient_scope`.**", 1)[1])
    assert "--scopes <scopes>" in step3 and '--scope "<scope>"' in step3
    assert "--scopes memory:read,memory:write,offline_access" in step3, "worked example"
    for path in (CLIENT_DOCS, TROUBLESHOOTING, CODEX_SKILL):
        assert "--scopes memory:read,memory:write`" not in _read(path), path.name


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        (_login, "in place of `kagura-memory` in every instruction and command you hand the user"),
        (_codex_login, "in place of `kagura-memory` in every command below"),
    ],
    ids=["claude", "codex"],
)
def test_both_skills_use_the_detected_entry_name(text, needle: str) -> None:
    """An entry under another name must be the one signed in (PR #1712 review)."""
    assert needle in _flat(text())


def test_cli_profile_login_without_a_profile_row_takes_the_entry_server() -> None:
    step2 = _flat(_section(_login(), "### CLI profile (`kagura-mcp`)"))
    assert "the entry's `--server` argument" in step2


def test_bearer_rotation_keeps_the_entry_other_headers() -> None:
    bearer = _flat(_section(_login(), "### Bearer key"))
    assert "every other `--header` the old entry had" in bearer


# ---------------------------------------------------------------------------
# /login is Claude Code's Anthropic sign-in, never Kagura's
# ---------------------------------------------------------------------------

BARE_LOGIN = re.compile(r"(?<![\w:/.-])/login\b")


@pytest.mark.parametrize("path", LOGIN_DOCS, ids=lambda p: p.name)
def test_no_doc_suggests_the_bare_login_command(path: Path) -> None:
    for paragraph in re.split(r"\n\s*\n", _read(path)):
        if BARE_LOGIN.search(paragraph):
            assert "Anthropic" in paragraph, f"{path.name}: bare /login outside the warning"


def test_the_login_skill_warns_about_the_builtin_login() -> None:
    flat = _flat(_login())
    assert "This is `/kagura-memory:login`" in flat
    assert "built-in `/login` signs in to the Anthropic account" in flat


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------


def test_client_docs_point_to_the_login_skill() -> None:
    section = _flat(_section(_read(CLIENT_DOCS), "## Sign in again"))
    for needle in (
        "`/kagura-memory:login`",
        "invalid_token",
        "insufficient_scope",
        "codex mcp login kagura-memory",
        "Anthropic",
        "(mcp-tools.md#oauth-scopes)",
    ):
        assert needle in section, needle


def test_guide_points_to_the_login_skill() -> None:
    guide = _read(GUIDE)
    step1 = _flat(_section(guide, "### 1. Check MCP connection"))
    for needle in ("`/kagura-memory:login`", "invalid_token", "insufficient_scope"):
        assert needle in step1, needle


def test_troubleshooting_points_to_the_login_skill() -> None:
    section = _flat(
        _section(
            _read(TROUBLESHOOTING),
            "## A Kagura tool fails with `invalid_token` or `insufficient_scope`",
        )
    )
    for needle in (
        "`/kagura-memory:login`",
        "codex mcp login kagura-memory",
        "(mcp-clients.md#sign-in-again)",
        "(mcp-tools.md#oauth-scopes)",
        "Anthropic",
    ):
        assert needle in section, needle
