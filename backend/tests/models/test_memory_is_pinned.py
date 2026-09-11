"""#1519: one Python-side definition of "pinned" — Memory.is_pinned."""

from models.memory import Memory


def test_is_pinned_true_for_always():
    assert Memory(delivery_mode="always").is_pinned is True


def test_is_pinned_false_otherwise():
    assert Memory(delivery_mode="on_recall").is_pinned is False
    assert Memory().is_pinned is False  # column default is on_recall
