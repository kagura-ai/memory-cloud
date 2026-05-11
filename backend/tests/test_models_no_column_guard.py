"""Negative-case smoke test for the `lint-models-no-column` CI guard (#596).

The guard's purpose is to prevent regression to the legacy SQLAlchemy 1.x
``Column()`` pattern after the #370 migration to ``Mapped[T] = mapped_column()``
completes. There are two forms the regex must catch:

1. **Bare**: ``name = Column(...)`` — the pre-PR-A style.
2. **Annotated half-migration**: ``id: int = Column(...)`` — type-annotated
   but still using ``Column()``. pyright accepts this (no type-checker
   warning) but SQLAlchemy 2.0 does NOT recognize it as a ``Mapped`` attribute,
   so it silently fails ORM behavior (no instrumentation, no relationship
   resolution, etc.). See memory ``fcc7cf87`` for the original discovery.

Without explicit coverage for the annotated form, a reviewer would assume
"pyright is green, must be fine" and miss the latent bug. These tests run as
part of the normal unit suite so the contract is verified on every CI run,
not only when someone manually invokes ``make lint-models-no-column``.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

# Must match the regex in the Makefile lint-models-no-column target exactly.
# POSIX character classes ([[:space:]] / [[:alnum:]_]) are used instead of
# GNU shorthand (\s / \w) so the guard is portable to BSD grep (macOS dev).
GUARD_REGEX = r"^[[:space:]]+[[:alnum:]_]+(:[[:space:]]*[^=]+)?[[:space:]]*=[[:space:]]*Column\("


def _grep_returncode(content: str, tmp_path: Path) -> int:
    """Run the guard regex against ``content`` in a tmpdir and return the
    ``grep`` exit code.

    grep exits 0 if any match is found (= guard would fail in CI),
    exits 1 if no match is found (= guard passes).
    """
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "fake_model.py").write_text(content)
    # ``grep -rnE`` is what the Makefile target invokes. We use the same
    # flags for parity, even though for a single file ``-r`` is redundant.
    result = subprocess.run(
        ["grep", "-rnE", GUARD_REGEX, str(models_dir) + "/"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode


@pytest.fixture(scope="module")
def grep_available() -> None:
    """Skip the suite cleanly if ``grep`` is not on PATH (unlikely on any
    Linux CI runner; defensive against future Windows runners)."""
    if shutil.which("grep") is None:
        pytest.skip("grep not available on PATH")


# ---------------------------------------------------------------------------
# Positive cases — the guard MUST flag these
# ---------------------------------------------------------------------------


def test_bare_column_form_is_caught(tmp_path: Path, grep_available: None) -> None:
    """The original pre-migration form ``name = Column(...)`` must trigger."""
    content = textwrap.dedent(
        """\
        from sqlalchemy import Column, String

        class FakeModel:
            __tablename__ = "fake"
            name = Column(String(255), nullable=False)
        """
    )
    assert _grep_returncode(content, tmp_path) == 0, (
        "Guard regex did not match the bare 'name = Column(...)' form. "
        "This is the primary case the guard exists to catch."
    )


def test_annotated_half_migration_form_is_caught(tmp_path: Path, grep_available: None) -> None:
    """The annotated half-migration form ``id: int = Column(...)`` must
    trigger.

    This is the memory ``fcc7cf87`` latent bug: pyright sees the ``int``
    annotation and reports no error, but SA 2.0 does not recognize the
    attribute as a ``Mapped`` column. The guard's main value-add over a
    naive ``= Column(`` regex is catching this form.
    """
    content = textwrap.dedent(
        """\
        from sqlalchemy import Column, Integer

        class FakeModel:
            __tablename__ = "fake"
            id: int = Column(Integer, primary_key=True)
        """
    )
    assert _grep_returncode(content, tmp_path) == 0, (
        "Guard regex did not match the annotated 'id: int = Column(...)' "
        "half-migration form. The regex must be wide enough that "
        "pyright-clean half-migrations cannot slip through."
    )


def test_annotated_with_optional_type_is_caught(tmp_path: Path, grep_available: None) -> None:
    """Variant with a complex type annotation: ``user_id: str | None = Column(...)``.

    Ensures the ``(:\\s*[^=]+)?`` group accepts arbitrary type expressions
    between the name and the ``=``, not just simple identifiers.
    """
    content = textwrap.dedent(
        """\
        from sqlalchemy import Column, String

        class FakeModel:
            __tablename__ = "fake"
            user_id: str | None = Column(String(255), nullable=True)
        """
    )
    assert _grep_returncode(content, tmp_path) == 0, (
        "Guard regex did not match the union-typed half-migration form. "
        "The type-annotation group must accept '|' (PEP 604 unions) — "
        "matching only simple identifiers would let half-migrations slip "
        "through any time the column type is nullable."
    )


# ---------------------------------------------------------------------------
# Negative cases — the guard must NOT flag these
# ---------------------------------------------------------------------------


def test_modern_mapped_column_form_is_not_caught(tmp_path: Path, grep_available: None) -> None:
    """The target ``Mapped[T] = mapped_column(...)`` form must pass cleanly.

    Without this counter-test, a regression in the regex (e.g. dropping
    the ``Column\\(`` literal) would still pass the positive cases but
    start false-positiving on every model file in the repo.
    """
    content = textwrap.dedent(
        """\
        from sqlalchemy import Integer, String
        from sqlalchemy.orm import Mapped, mapped_column

        class FakeModel:
            __tablename__ = "fake"
            id: Mapped[int] = mapped_column(Integer, primary_key=True)
            name: Mapped[str] = mapped_column(String(255), nullable=False)
        """
    )
    assert _grep_returncode(content, tmp_path) == 1, (
        "Guard regex false-positived on the target 'mapped_column(...)' "
        "form. A passing positive case is not enough — the regex must "
        "also be precise enough that every migrated model in the repo "
        "lints clean."
    )


def test_column_import_alone_is_not_caught(tmp_path: Path, grep_available: None) -> None:
    """``from sqlalchemy import Column`` (import only, no usage) must pass.

    Some modules import ``Column`` for non-column purposes (e.g. inside
    ``CheckConstraint`` SQL strings, or for type-only references). The
    regex anchors on indented ``\\s+\\w+\\s*=\\s*Column\\(`` so a bare
    import line cannot trigger it.
    """
    content = textwrap.dedent(
        """\
        from sqlalchemy import Column  # noqa: F401  unused — used in CheckConstraint below

        # ... no column assignment ...
        """
    )
    assert _grep_returncode(content, tmp_path) == 1, (
        "Guard regex false-positived on a bare 'from sqlalchemy import "
        "Column' line. The regex must require an indented '<name> = "
        "Column(...)' assignment, not just the word 'Column' anywhere "
        "in the file."
    )
