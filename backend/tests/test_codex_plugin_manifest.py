"""Guard tests for #802: Codex plugin manifest integrity + version lockstep.

The Codex plugin ships three coupled artifacts at the repo root:

* ``.agents/plugins/marketplace.json``                  — Codex marketplace manifest
* ``plugins/kagura-memory/.codex-plugin/plugin.json``    — Codex plugin manifest
* ``plugins/kagura-memory/skills/*/SKILL.md``            — Codex skill(s)

These tests pin the invariants that ``codex plugin add
kagura-memory@kagura-memory-cloud`` depends on (the release process in
``.claude/commands/release.md`` bumps the manifests by hand;
``tests/test_release_version_lockstep.py`` guards the other version files):

1. The Codex plugin version equals the canonical ``APP_VERSION`` and the Claude
   plugin manifest version — a release that bumps one manifest but forgets the
   other fails here (mirrors ``frontend/src/lib/version.test.ts``).
2. The marketplace/plugin name pair spells the ``<plugin>@<marketplace>``
   install handle from the acceptance criteria.
3. Every path the manifests reference (plugin source dir, skills dir, icons)
   resolves to a real file, using Codex's path-resolution roots: marketplace
   ``source.path`` resolves relative to the repo root; plugin ``skills`` /
   ``interface.logo`` / ``interface.composerIcon`` resolve relative to the
   plugin root. A moved/renamed asset that silently drops the plugin from
   Codex fails here instead.
4. Each ``SKILL.md`` carries the YAML frontmatter (``name`` + ``description``)
   Codex requires to register the skill.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

from config.constants import APP_VERSION

# backend/tests/test_codex_plugin_manifest.py -> tests -> backend -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MARKETPLACE = _REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
_CLAUDE_PLUGIN = _REPO_ROOT / ".claude-plugin" / "plugin.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _marketplace_plugin_entry() -> dict:
    plugins = _load(_MARKETPLACE)["plugins"]
    assert plugins, "marketplace manifest lists no plugins"
    return plugins[0]


def _plugin_root() -> Path:
    """Resolve the plugin dir the way Codex does: marketplace ``source.path``
    is relative to the marketplace/repo root, not to ``.agents/plugins/``.
    """
    rel = _marketplace_plugin_entry()["source"]["path"]
    root = (_REPO_ROOT / rel).resolve()
    assert root.is_dir(), f"plugin source path does not resolve to a dir: {rel}"
    return root


def _codex_manifest() -> dict:
    return _load(_plugin_root() / ".codex-plugin" / "plugin.json")


def _parse_frontmatter(path: Path) -> dict[str, str]:
    """Minimal YAML-frontmatter reader (single-line ``key: value`` pairs).

    Avoids a PyYAML dependency; SKILL.md frontmatter is flat ``key: value``.
    """
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), f"{path} is missing YAML frontmatter"
    # Frontmatter is the block between the first two ``---`` fences.
    _, block, _ = text.split("---", 2)
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def test_codex_plugin_version_matches_app_version() -> None:
    """Codex manifest version is in lockstep with the canonical runtime version."""
    assert _codex_manifest()["version"] == APP_VERSION


def test_codex_and_claude_plugin_versions_match() -> None:
    """Both plugin manifests carry the same version (release.md lockstep rule)."""
    assert _codex_manifest()["version"] == _load(_CLAUDE_PLUGIN)["version"]


def test_install_handle_names_are_consistent() -> None:
    """``kagura-memory@kagura-memory-cloud`` only resolves if these names align."""
    marketplace = _load(_MARKETPLACE)
    entry = marketplace["plugins"][0]
    assert marketplace["name"] == "kagura-memory-cloud"
    assert entry["name"] == "kagura-memory"
    # The plugin manifest's own name must match the marketplace entry's name,
    # otherwise the install handle points at a plugin that won't load.
    assert _codex_manifest()["name"] == entry["name"]


def test_codex_manifest_referenced_paths_exist() -> None:
    """Skills dir and interface icons resolve to real files under the plugin root."""
    plugin_root = _plugin_root()
    manifest = _codex_manifest()

    skills_dir = (plugin_root / manifest["skills"]).resolve()
    assert skills_dir.is_dir(), f"skills path does not exist: {manifest['skills']}"
    assert list(skills_dir.glob("*/SKILL.md")), "no SKILL.md found under skills dir"

    interface = manifest["interface"]
    for field in ("logo", "composerIcon"):
        asset = (plugin_root / interface[field]).resolve()
        assert asset.is_file(), f"interface.{field} does not exist: {interface[field]}"
        # Codex rejects ``..`` traversal — keep assets inside the plugin root.
        assert plugin_root in asset.parents or asset.parent == plugin_root


def test_claude_plugin_hooks_path_resolves_and_names_the_shared_script() -> None:
    """The Claude manifest's ``hooks`` file exists inside its plugin root (the repo root)
    and every hook command runs the script shared with the Codex plugin (#1619)."""
    hooks_rel = _load(_CLAUDE_PLUGIN)["hooks"]
    hooks_file = (_REPO_ROOT / hooks_rel).resolve()
    assert hooks_file.is_file(), f"Claude hooks path does not resolve: {hooks_rel}"
    assert _REPO_ROOT.resolve() in hooks_file.parents
    commands = [
        hook["command"]
        for groups in _load(hooks_file)["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]
    assert commands
    script = "${CLAUDE_PLUGIN_ROOT}/plugins/kagura-memory/hooks/kagura_guardrails.py"
    for command in commands:
        assert script in command, command
    assert (_plugin_root() / "hooks" / "kagura_guardrails.py").is_file()


# ---------------------------------------------------------------------------
# Codex hooks (#1620): ``plugins/kagura-memory/hooks/hooks.json`` + the manifest ``hooks`` key
# ---------------------------------------------------------------------------

# Changing hooks.json changes the trust hash of every hook entry in it (Codex hashes the
# event, the matcher and the handler config), which sends every user back through
# ``/hooks`` before the hooks run again. Bump this constant only on purpose; the failing
# assertion prints the new hash.
HOOKS_JSON_SHA256 = "f64dde5a8224e34ee4e88bf62627a34896e7893a47d124776bd20e757da4cd32"

# The Codex form of the sh guard (decisions §2.2): absolute interpreter outside ``$PWD``,
# ``-I -S``, the script under ``$PLUGIN_ROOT`` — the variable Codex exports to the hook
# process, expanded by the shell inside double quotes. Never the braced ``${PLUGIN_ROOT}``:
# Codex substitutes that form textually into the command before ``$SHELL -lc`` runs
# (``hooks/src/engine/discovery.rs``), so a path with ``$(``, backticks or ``"`` would
# become shell syntax.
CODEX_GUARD_COMMAND = (
    'p=$(command -v python3) || exit 0; case "$p" in /*) ;; *) exit 0;; esac; '
    'case "$p" in "$PWD"/*) exit 0;; esac; '
    'exec "$p" -I -S "$PLUGIN_ROOT/hooks/kagura_guardrails.py" --client codex'
)
REFRESH_MATCHER = "^mcp__.*__(remember|update_memory|forget)$"


def _codex_hooks_path() -> Path:
    return _plugin_root() / "hooks" / "hooks.json"


def _codex_hooks() -> dict:
    return _load(_codex_hooks_path())


def _codex_handlers() -> list[tuple[str, str | None, dict]]:
    """``(event, matcher, handler)`` for every handler in the Codex hooks file."""
    return [
        (event, group.get("matcher"), handler)
        for event, groups in _codex_hooks()["hooks"].items()
        for group in groups
        for handler in group["hooks"]
    ]


def test_codex_manifest_hooks_path_resolves_inside_plugin_root() -> None:
    """``hooks`` starts with ``./``, resolves to a file inside the plugin root (Codex rejects
    ``..`` and paths outside the root) and names the default location too, so both discovery
    routes yield one trust key."""
    hooks_rel = _codex_manifest()["hooks"]
    assert hooks_rel == "./hooks/hooks.json"
    assert hooks_rel.startswith("./") and ".." not in hooks_rel
    resolved = (_plugin_root() / hooks_rel).resolve()
    assert resolved.is_file(), f"Codex hooks path does not resolve: {hooks_rel}"
    assert _plugin_root().resolve() in resolved.parents
    assert resolved == _codex_hooks_path().resolve()


def test_codex_hooks_file_sha256_is_pinned() -> None:
    """Every byte of hooks.json is deliberate: see the comment on ``HOOKS_JSON_SHA256``."""
    raw = _codex_hooks_path().read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == HOOKS_JSON_SHA256, (
        "plugins/kagura-memory/hooks/hooks.json changed. A changed matcher or handler sends every "
        "Codex user back through /hooks; if the change is intended, set HOOKS_JSON_SHA256 = "
        f"{digest!r}"
    )
    assert raw.endswith(b"\n") and b"\r" not in raw
    assert json.loads(raw) == json.loads(json.dumps(json.loads(raw), indent=2)), "two-space indent"


def test_codex_hooks_file_shape() -> None:
    data = _codex_hooks()
    assert set(data) <= {"description", "hooks"}
    assert set(data["hooks"]) == {"SessionStart", "PreToolUse", "PostToolUse"}
    assert "trust hash" in data["description"] and "SHA-256" in data["description"]
    for event, matcher, handler in _codex_handlers():
        assert handler["type"] == "command", event
        assert handler["commandWindows"] == "exit 0", event
        assert isinstance(handler["timeout"], int) and 0 < handler["timeout"] <= 10, event
        assert "additionalContextLimit" not in handler, event
        assert "args" not in handler and "${user_config" not in handler["command"]
        expected = CODEX_GUARD_COMMAND + (" --refresh" if handler.get("async") else "")
        assert handler["command"] == expected, (event, matcher)
        assert '"$PLUGIN_ROOT/hooks/kagura_guardrails.py"' in handler["command"]
        assert "${" not in handler["command"], "Codex would substitute it textually"
        if "async" in handler:
            assert isinstance(handler["async"], bool), event
        if event == "SessionStart":
            assert matcher is None
            assert handler["statusMessage"] == "Loading Kagura Memory guardrails"
        else:
            assert "statusMessage" not in handler, event
    assert (_plugin_root() / "hooks" / "kagura_guardrails.py").is_file()


def test_codex_hooks_timeouts_and_matchers() -> None:
    handlers = _codex_handlers()
    by_event: dict[str, list[tuple[str | None, dict]]] = {}
    for event, matcher, handler in handlers:
        by_event.setdefault(event, []).append((matcher, handler))
    assert [h["timeout"] for _, h in by_event["SessionStart"]] == [5]
    assert [(m, h["timeout"]) for m, h in by_event["PreToolUse"]] == [("*", 5)]
    post = by_event["PostToolUse"]
    assert len(post) == 2
    assert post[0][0] == "*" and post[0][1]["timeout"] == 5 and not post[0][1].get("async")
    assert post[1][0] == REFRESH_MATCHER
    assert post[1][1]["async"] is True and post[1][1]["timeout"] == 10
    async_handlers = [h for _, _, h in handlers if h.get("async") is True]
    assert len(async_handlers) == 1 and async_handlers[0]["command"].endswith(" --refresh")


def test_refresh_matcher_is_a_regex_for_codex_and_compiles() -> None:
    """Codex treats a matcher made only of ``[A-Za-z0-9_|]`` as an exact name list; the refresh
    matcher must carry another character so it is compiled as a regex."""
    assert re.search(r"[^A-Za-z0-9_|]", REFRESH_MATCHER)
    compiled = re.compile(REFRESH_MATCHER)
    assert compiled.search("mcp__kagura-memory__remember")
    assert compiled.search("mcp__kagura-memory__update_memory")
    assert not compiled.search("mcp__kagura-memory__recall")


@pytest.mark.parametrize(
    "hooks_file",
    [
        _REPO_ROOT / "claude-hooks" / "hooks.json",
        _REPO_ROOT / "plugins" / "kagura-memory" / "hooks" / "hooks.json",
    ],
    ids=["claude", "codex"],
)
def test_both_hooks_files_parse_under_codex_rules(hooks_file: Path) -> None:
    """Codex's ``HooksFile`` is ``deny_unknown_fields`` (top level: ``description``, ``hooks``)
    while its event map is not, so the Claude file (with ``PostToolUseFailure``) loads under
    Codex on the legacy-marketplace path; every handler must still be a well-typed command."""
    data = _load(hooks_file)
    assert set(data) <= {"description", "hooks"}, hooks_file
    for event, groups in data["hooks"].items():
        for group in groups:
            assert set(group) <= {"matcher", "hooks"}, event
            for handler in group["hooks"]:
                assert handler["type"] == "command", event
                if "timeout" in handler:
                    assert isinstance(handler["timeout"], int), event
                if "async" in handler:
                    assert isinstance(handler["async"], bool), event
                assert isinstance(handler["command"], str) and handler["command"], event


def test_every_skill_has_required_frontmatter() -> None:
    """Every SKILL.md declares the ``name`` + ``description`` Codex needs to register it."""
    skills_dir = (_plugin_root() / _codex_manifest()["skills"]).resolve()
    skill_files = sorted(skills_dir.glob("*/SKILL.md"))
    assert skill_files, "no SKILL.md files found"
    for skill in skill_files:
        fields = _parse_frontmatter(skill)
        assert fields.get("name"), f"{skill} frontmatter missing 'name'"
        assert fields.get("description"), f"{skill} frontmatter missing 'description'"
