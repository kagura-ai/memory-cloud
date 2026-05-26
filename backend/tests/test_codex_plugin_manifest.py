"""Guard tests for #802: Codex plugin manifest integrity + version lockstep.

The Codex plugin ships three coupled artifacts at the repo root:

* ``.agents/plugins/marketplace.json``                  — Codex marketplace manifest
* ``plugins/kagura-memory/.codex-plugin/plugin.json``    — Codex plugin manifest
* ``plugins/kagura-memory/skills/*/SKILL.md``            — Codex skill(s)

These tests pin the invariants that ``codex plugin add
kagura-memory@kagura-memory-cloud`` depends on, and that the release process
(``.claude/commands/release.md``) currently keeps in lockstep only
procedurally:

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

import json
from pathlib import Path

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


def test_every_skill_has_required_frontmatter() -> None:
    """Every SKILL.md declares the ``name`` + ``description`` Codex needs to register it."""
    skills_dir = (_plugin_root() / _codex_manifest()["skills"]).resolve()
    skill_files = sorted(skills_dir.glob("*/SKILL.md"))
    assert skill_files, "no SKILL.md files found"
    for skill in skill_files:
        fields = _parse_frontmatter(skill)
        assert fields.get("name"), f"{skill} frontmatter missing 'name'"
        assert fields.get("description"), f"{skill} frontmatter missing 'description'"
