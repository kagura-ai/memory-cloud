"""The docs' ``Cookie:`` examples name the session cookie the server reads.

An example that sends another name answers 401 when an operator copies it.
"""

from __future__ import annotations

import re
from pathlib import Path

from auth.session import SESSION_COOKIE_NAME

REPO = Path(__file__).resolve().parents[2]
_COOKIE = re.compile(r"Cookie: ([A-Za-z0-9_.-]+)=")


def _documents() -> list[Path]:
    return sorted([*REPO.joinpath("docs").rglob("*.md"), REPO / "README.md"])


def test_every_cookie_example_names_the_session_cookie() -> None:
    wrong = [
        f"{path.relative_to(REPO)}:{number}: {name}"
        for path in _documents()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for name in _COOKIE.findall(line)
        if name != SESSION_COOKIE_NAME
    ]
    assert wrong == []
