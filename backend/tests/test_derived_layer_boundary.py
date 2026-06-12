"""Executable guard for the raw-vs-derived data boundary (Issue #968).

Moat lever M2: raw memories (user-authored text, tags, provenance) are
exportable; the derived/learned layer (Hebbian edge weights, embedding
calibration, Sleep-consolidated structure) is the moat and must never leak
into the raw-export surface.

The boundary itself lives in two places, and this module pins both:

1. ``models.data_boundary`` — the machine-readable classification registry.
   Every ORM table must be classified into exactly one bucket (raw /
   derived / operational), so any NEW table fails this suite until its
   author makes the boundary call explicitly.
2. ``docs/derived-layer-boundary.md`` — the design doc stating the rule,
   enumerating the boundary-relevant artifacts, AND carrying the
   feature-review checklist (the two boundary questions asked on every
   storage/export/Sleep/edge change). The doc-drift tests keep it in sync
   with the registry. (The checklist previously lived in the now-retired
   ``.claude/commands/self-review.md``; the canonical home is this doc — see
   issue #996.)
"""

from __future__ import annotations

from pathlib import Path

# Import every model module so Base.metadata is fully populated, without
# relying on the side-effect imports inside models/__init__.py (#531). The
# explicit list keeps this guard self-sufficient: a table only escapes
# classification if its module is imported nowhere at all. Same canonical
# module set as tests/test_schema_drift.py's _MODEL_MODULES.
import models  # noqa: F401
import models.agent_state  # noqa: F401
import models.analysis  # noqa: F401
import models.auth  # noqa: F401
import models.bm25_drift  # noqa: F401
import models.config  # noqa: F401
import models.erasure  # noqa: F401
import models.file_objects  # noqa: F401
import models.hub_tag  # noqa: F401
import models.llm_call_log  # noqa: F401
import models.llm_pricing  # noqa: F401
import models.memory  # noqa: F401
import models.neural  # noqa: F401
import models.resource  # noqa: F401
import models.retrieval_feedback  # noqa: F401
import models.signup_gate  # noqa: F401
import models.sleep  # noqa: F401
from db.base import Base
from models import schemas
from models.data_boundary import (
    DERIVED_MOAT_TABLES,
    DERIVED_ONLY_FIELD_NAMES,
    EXPORT_SURFACE_FIELD_EXCEPTIONS,
    EXPORT_SURFACE_SCHEMA_NAMES,
    OPERATIONAL_TABLES,
    RAW_EXPORTABLE_TABLES,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DESIGN_DOC = REPO_ROOT / "docs" / "derived-layer-boundary.md"


class TestTableClassification:
    """Every ORM table must carry an explicit boundary classification."""

    def test_every_table_is_classified(self) -> None:
        actual = set(Base.metadata.tables.keys())
        classified = RAW_EXPORTABLE_TABLES | DERIVED_MOAT_TABLES | OPERATIONAL_TABLES

        unclassified = actual - classified
        assert not unclassified, (
            f"Tables missing a boundary classification in models/data_boundary.py: "
            f"{sorted(unclassified)}. For each new table decide: is it raw "
            f"user-authored content (RAW_EXPORTABLE_TABLES), learned/derived "
            f"structure (DERIVED_MOAT_TABLES), or platform plumbing "
            f"(OPERATIONAL_TABLES)? See docs/derived-layer-boundary.md."
        )

        stale = classified - actual
        assert not stale, (
            f"models/data_boundary.py classifies tables that no longer exist: "
            f"{sorted(stale)}. Remove them from the registry (and from "
            f"docs/derived-layer-boundary.md)."
        )

    def test_classifications_are_disjoint(self) -> None:
        assert not RAW_EXPORTABLE_TABLES & DERIVED_MOAT_TABLES
        assert not RAW_EXPORTABLE_TABLES & OPERATIONAL_TABLES
        assert not DERIVED_MOAT_TABLES & OPERATIONAL_TABLES


class TestExportSurface:
    """Derived-only signal must never appear on user-facing memory schemas."""

    def test_export_surface_schemas_exist(self) -> None:
        missing = [name for name in EXPORT_SURFACE_SCHEMA_NAMES if not hasattr(schemas, name)]
        assert not missing, (
            f"EXPORT_SURFACE_SCHEMA_NAMES references schemas that do not exist "
            f"in models/schemas.py: {missing}"
        )

    def test_no_derived_fields_on_export_schemas(self) -> None:
        leaks: dict[str, list[str]] = {}
        for name in EXPORT_SURFACE_SCHEMA_NAMES:
            schema_cls = getattr(schemas, name)
            allowed = EXPORT_SURFACE_FIELD_EXCEPTIONS.get(name, frozenset())
            leaked = sorted((set(schema_cls.model_fields) & DERIVED_ONLY_FIELD_NAMES) - allowed)
            if leaked:
                leaks[name] = leaked
        assert not leaks, (
            f"Derived-layer fields leaked onto the raw-export surface: {leaks}. "
            f"Edge weight/origin, calibration percentiles, and Sleep "
            f"consolidation results are the moat — they must not be exported. "
            f"See docs/derived-layer-boundary.md."
        )


class TestDesignDoc:
    """docs/derived-layer-boundary.md must exist and stay in sync."""

    def test_doc_exists(self) -> None:
        assert DESIGN_DOC.is_file(), (
            "docs/derived-layer-boundary.md is missing — it is the canonical "
            "statement of the raw-vs-derived boundary (Issue #968)."
        )

    def test_doc_states_the_rule(self) -> None:
        text = DESIGN_DOC.read_text(encoding="utf-8").lower()
        for keyword in ("raw", "exportable", "derived", "moat", "compound"):
            assert keyword in text, (
                f"Design doc no longer states the boundary rule (missing keyword {keyword!r})."
            )

    def test_doc_enumerates_boundary_tables(self) -> None:
        """Every raw and derived table must be named in the doc (drift guard).

        Operational tables are exempt — they are listed only in the registry.
        """
        text = DESIGN_DOC.read_text(encoding="utf-8")
        missing = [
            t for t in sorted(RAW_EXPORTABLE_TABLES | DERIVED_MOAT_TABLES) if f"`{t}`" not in text
        ]
        assert not missing, (
            f"Boundary-relevant tables not documented in "
            f"docs/derived-layer-boundary.md: {missing}. When classifying a "
            f"table as raw or derived, add it to the doc's enumeration."
        )


class TestFeatureReviewChecklist:
    """The design doc must carry the two boundary questions (Issue #968 item 2)."""

    def test_checklist_has_boundary_section(self) -> None:
        text = DESIGN_DOC.read_text(encoding="utf-8")
        marker = "## Feature-review checklist"
        assert marker in text, (
            "docs/derived-layer-boundary.md lost the Feature-review checklist "
            "section — the two boundary questions reviewers must answer on "
            "every storage/export/Sleep/edge change."
        )
        # Scope the assertions to the checklist section itself (not the whole
        # doc, where 'export'/'derived' appear everywhere).
        section = text.split(marker, 1)[1].split("\n## ", 1)[0].lower()
        # (a) no derived signal on the raw-export surface
        assert "export" in section and "derived" in section
        # (b) derived signal genuinely accrues / compounds with use
        assert "accru" in section or "compound" in section
