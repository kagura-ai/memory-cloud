"""Tests for utils/error_messages.py.

Pure constants — a single test mechanically covers all lines via
introspection so newly added constants are picked up automatically
(no manual list to keep in sync).
"""

from utils.error_messages import ErrorMessages


def test_all_public_error_messages_are_non_empty_strings():
    """Every UPPER_SNAKE_CASE class attribute on ``ErrorMessages`` must
    be a non-empty string. Iterating via ``vars()`` rather than a hand-
    maintained list ensures that adding a new constant to
    ``ErrorMessages`` immediately gains coverage — without iteration,
    the previous test silently missed any newly added constant because
    its hard-coded list never grew.
    """
    public_attrs = {
        name: value
        for name, value in vars(ErrorMessages).items()
        if name.isupper() and not name.startswith("_")
    }
    assert public_attrs, (
        "ErrorMessages exposes no UPPER_SNAKE_CASE public constants — "
        "either the class is empty or the discovery convention changed."
    )
    for name, msg in public_attrs.items():
        assert isinstance(msg, str), f"{name} must be a str, got {type(msg).__name__}"
        assert len(msg) > 0, f"{name} must be a non-empty string"
