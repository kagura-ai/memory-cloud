"""#1519: one Python-side definition of "pinned" — Memory.is_pinned.
#1523: one SQL definition — pinned_predicate() / not_pinned_predicate()."""

from models.memory import (
    DELIVERY_MODE_ALWAYS,
    Memory,
    not_pinned_predicate,
    pinned_predicate,
)


def test_is_pinned_true_for_always():
    assert Memory(delivery_mode="always").is_pinned is True


def test_is_pinned_false_otherwise():
    assert Memory(delivery_mode="on_recall").is_pinned is False
    assert Memory().is_pinned is False  # column default is on_recall


def _sql(clause) -> str:
    return str(clause.compile(compile_kwargs={"literal_binds": True}))


def test_pinned_predicate_is_the_always_lane():
    assert _sql(pinned_predicate()) == "memories.delivery_mode = 'always'"


def test_not_pinned_predicate_is_the_exact_complement():
    """delivery_mode is NOT NULL, so the plain inequality is the full complement —
    no NULL branch that could let a row fall out of both sides."""
    assert _sql(not_pinned_predicate()) == "memories.delivery_mode != 'always'"


def test_python_and_sql_definitions_share_the_constant():
    """A rename of the sentinel must move both readers at once."""
    assert Memory(delivery_mode=DELIVERY_MODE_ALWAYS).is_pinned is True
    assert DELIVERY_MODE_ALWAYS in _sql(pinned_predicate())
