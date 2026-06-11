"""Executable guard for the raw-vs-derived data boundary (Issue #968).

Moat lever M2: raw memories (user-authored text, tags, provenance) are
exportable; the derived/learned layer (Hebbian edge weights, embedding
calibration, Sleep-consolidated structure) is the moat and must never leak
into the raw-export surface.

The boundary itself lives in three places, and this module pins all of them:

1. ``models.data_boundary`` — the machine-readable classification registry.
   Every ORM table must be classified into exactly one bucket (raw /
   derived / operational), so any NEW table fails this suite until its
   author makes the boundary call explicitly.
2. ``docs/derived-layer-boundary.md`` — the design doc stating the rule and
   enumerating the boundary-relevant artifacts. The doc-drift tests keep it
   in sync with the registry.
3. ``.claude/commands/self-review.md`` — the feature-review checklist item
   that asks the two boundary questions on every storage/export/Sleep/edge
   change.
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
SELF_REVIEW_CHECKLIST = REPO_ROOT / ".claude" / "commands" / "self-review.md"


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
    """self-review must ask the two boundary questions (Issue #968 item 2)."""

    def test_checklist_has_boundary_section(self) -> None:
        text = SELF_REVIEW_CHECKLIST.read_text(encoding="utf-8")
        assert "derived-layer-boundary" in text, (
            ".claude/commands/self-review.md lost the derived-layer boundary "
            "checklist item (must reference docs/derived-layer-boundary.md)."
        )
        # The two mandated questions: (a) no derived signal on the raw-export
        # surface, (b) derived signal genuinely accrues with use.
        lowered = text.lower()
        assert "export" in lowered and "derived" in lowered
        assert "accrue" in lowered or "compound" in lowered
