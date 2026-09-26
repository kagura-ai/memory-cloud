"""``is_utf8_encodable`` (Issue #1718).

``"\\ud800"`` is a legal JSON escape, so a request body can hand the app a
``str`` holding a lone surrogate. ``str.encode()`` raises on it. The helper
answers the question without raising, so callers can treat such input as a
wrong credential instead of a 500.
"""

from __future__ import annotations

import pytest

from utils.utf8 import is_utf8_encodable


@pytest.mark.parametrize(
    "value",
    ["", "ascii", "パスワード", "emoji \U0001f600", "\x00control"],
)
def test_encodable_text(value: str) -> None:
    assert is_utf8_encodable(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "\ud800",  # lone high surrogate
        "\udc00",  # lone low surrogate
        "abc\udcff",  # os.environ's surrogateescape form of byte 0xff
        chr(0xD83D) + chr(0xDE00),  # a surrogate pair as two code points is still unencodable
    ],
)
def test_lone_surrogates_are_not_encodable(value: str) -> None:
    assert is_utf8_encodable(value) is False
