"""Coverage tests for the MCP tool description i18n service.

Exercises ``mcp_server.i18n.MCPToolDescriptionService`` against a real
``db_session`` (per-session throwaway DB). Covers:

- ``get_descriptions(locale)`` — locale filtering and the empty case.
- ``get_description`` — direct hit, ja->en fallback (warning path), and the
  missing-everything -> None branch.
- ``update_description`` — both the create and update branches.
- ``list_all_descriptions`` — (tool_name, locale) ordering.

The ``(tool_name, locale)`` pair is unique, so each test uses a unique
``tool_name`` prefix (uuid-derived) to stay isolated within the shared
session-scoped database.
"""

import uuid

import pydantic.root_model  # noqa: F401  isort: skip  # pre-load so mcp.types' RootModel generic submodel resolves when this file is collected first
import pytest

from mcp_server.i18n import MCPToolDescriptionService
from models.auth import MCPToolDescription


def _unique_tool() -> str:
    """A tool_name guaranteed not to collide with other tests/rows."""
    return f"tool_{uuid.uuid4().hex[:12]}"


class TestGetDescriptions:
    """Tests for MCPToolDescriptionService.get_descriptions()."""

    async def test_returns_only_requested_locale(self, db_session):
        """get_descriptions filters by locale and maps tool_name -> description."""
        tool_a = _unique_tool()
        tool_b = _unique_tool()
        db_session.add_all(
            [
                MCPToolDescription(tool_name=tool_a, locale="en", description="A-en"),
                MCPToolDescription(tool_name=tool_b, locale="en", description="B-en"),
                MCPToolDescription(tool_name=tool_a, locale="ja", description="A-ja"),
            ]
        )
        await db_session.commit()

        service = MCPToolDescriptionService(db_session)
        en = await service.get_descriptions("en")

        assert en[tool_a] == "A-en"
        assert en[tool_b] == "B-en"
        # The ja-only mapping must not leak into the en result.
        assert en.get(tool_a) != "A-ja"

    async def test_returns_locale_specific_values(self, db_session):
        """A distinct locale yields its own description, not the en one."""
        tool = _unique_tool()
        db_session.add_all(
            [
                MCPToolDescription(tool_name=tool, locale="en", description="hello"),
                MCPToolDescription(tool_name=tool, locale="ja", description="こんにちは"),
            ]
        )
        await db_session.commit()

        service = MCPToolDescriptionService(db_session)
        ja = await service.get_descriptions("ja")

        assert ja[tool] == "こんにちは"

    async def test_unknown_locale_returns_empty_dict(self, db_session):
        """A locale with no rows yields an empty dict."""
        tool = _unique_tool()
        db_session.add(MCPToolDescription(tool_name=tool, locale="en", description="x"))
        await db_session.commit()

        service = MCPToolDescriptionService(db_session)
        result = await service.get_descriptions("zz")

        assert result == {}

    async def test_default_locale_is_en(self, db_session):
        """Calling without an explicit locale defaults to 'en'."""
        tool = _unique_tool()
        db_session.add(MCPToolDescription(tool_name=tool, locale="en", description="default-en"))
        await db_session.commit()

        service = MCPToolDescriptionService(db_session)
        result = await service.get_descriptions()

        assert result[tool] == "default-en"


class TestGetDescription:
    """Tests for MCPToolDescriptionService.get_description()."""

    async def test_direct_hit(self, db_session):
        """An exact (tool_name, locale) match returns its description."""
        tool = _unique_tool()
        db_session.add(MCPToolDescription(tool_name=tool, locale="en", description="direct"))
        await db_session.commit()

        service = MCPToolDescriptionService(db_session)
        assert await service.get_description(tool, "en") == "direct"

    async def test_ja_falls_back_to_en(self, db_session):
        """Missing ja falls back to the en row (exercises the warn+recurse path)."""
        tool = _unique_tool()
        db_session.add(MCPToolDescription(tool_name=tool, locale="en", description="fallback-en"))
        await db_session.commit()

        service = MCPToolDescriptionService(db_session)
        result = await service.get_description(tool, "ja")

        # The en row is returned via the locale!=en fallback recursion.
        assert result == "fallback-en"

    async def test_ja_present_no_fallback(self, db_session):
        """When the ja row exists, it is returned without falling back."""
        tool = _unique_tool()
        db_session.add_all(
            [
                MCPToolDescription(tool_name=tool, locale="en", description="en-val"),
                MCPToolDescription(tool_name=tool, locale="ja", description="ja-val"),
            ]
        )
        await db_session.commit()

        service = MCPToolDescriptionService(db_session)
        assert await service.get_description(tool, "ja") == "ja-val"

    async def test_missing_everything_returns_none(self, db_session):
        """No row in the requested locale nor en -> None."""
        service = MCPToolDescriptionService(db_session)
        result = await service.get_description(_unique_tool(), "ja")
        assert result is None

    async def test_missing_en_directly_returns_none(self, db_session):
        """Requesting en for a nonexistent tool returns None (no recursion)."""
        service = MCPToolDescriptionService(db_session)
        result = await service.get_description(_unique_tool(), "en")
        assert result is None


class TestUpdateDescription:
    """Tests for MCPToolDescriptionService.update_description()."""

    async def test_creates_when_absent(self, db_session):
        """First call for a (tool, locale) creates a new row (create branch)."""
        tool = _unique_tool()
        service = MCPToolDescriptionService(db_session)

        created = await service.update_description(
            tool_name=tool,
            locale="en",
            description="brand-new",
            user_id="admin-1",
        )

        assert isinstance(created, MCPToolDescription)
        assert created.id is not None
        assert created.description == "brand-new"

        # And it is actually persisted / retrievable.
        assert await service.get_description(tool, "en") == "brand-new"

    async def test_updates_when_present(self, db_session):
        """Second call for the same (tool, locale) updates in place (update branch)."""
        tool = _unique_tool()
        service = MCPToolDescriptionService(db_session)

        first = await service.update_description(tool, "en", "v1", "admin-1")
        original_id = first.id

        updated = await service.update_description(tool, "en", "v2", "admin-2")

        # Same row mutated, not a new insert.
        assert updated.id == original_id
        assert updated.description == "v2"
        assert await service.get_description(tool, "en") == "v2"

    async def test_distinct_locale_creates_separate_row(self, db_session):
        """Updating a different locale for the same tool makes a second row."""
        tool = _unique_tool()
        service = MCPToolDescriptionService(db_session)

        en_row = await service.update_description(tool, "en", "english", "admin")
        ja_row = await service.update_description(tool, "ja", "日本語", "admin")

        assert en_row.id != ja_row.id
        assert await service.get_description(tool, "en") == "english"
        assert await service.get_description(tool, "ja") == "日本語"


class TestListAllDescriptions:
    """Tests for MCPToolDescriptionService.list_all_descriptions()."""

    async def test_returns_all_rows(self, db_session):
        """Every inserted row appears in the listing."""
        tool = _unique_tool()
        db_session.add_all(
            [
                MCPToolDescription(tool_name=tool, locale="en", description="e"),
                MCPToolDescription(tool_name=tool, locale="ja", description="j"),
            ]
        )
        await db_session.commit()

        service = MCPToolDescriptionService(db_session)
        rows = await service.list_all_descriptions()

        mine = [r for r in rows if r.tool_name == tool]
        assert {r.locale for r in mine} == {"en", "ja"}

    async def test_ordered_by_tool_then_locale(self, db_session):
        """Results are ordered by tool_name, then locale."""
        # Two tools whose lexical order we control via a shared prefix.
        prefix = f"ord_{uuid.uuid4().hex[:8]}"
        tool_first = f"{prefix}_aaa"
        tool_second = f"{prefix}_bbb"
        db_session.add_all(
            [
                MCPToolDescription(tool_name=tool_second, locale="ja", description="2-ja"),
                MCPToolDescription(tool_name=tool_second, locale="en", description="2-en"),
                MCPToolDescription(tool_name=tool_first, locale="ja", description="1-ja"),
                MCPToolDescription(tool_name=tool_first, locale="en", description="1-en"),
            ]
        )
        await db_session.commit()

        service = MCPToolDescriptionService(db_session)
        rows = await service.list_all_descriptions()

        ours = [(r.tool_name, r.locale) for r in rows if r.tool_name.startswith(prefix)]
        assert ours == [
            (tool_first, "en"),
            (tool_first, "ja"),
            (tool_second, "en"),
            (tool_second, "ja"),
        ]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
