"""The accept refusal for an unverified address keeps the phrase the invite page reads (#2785).

``frontend/src/app/invite/[token]/page.tsx`` shows its own translated screen
for this refusal by finding ``UNVERIFIED_ADDRESS_MARKER`` in the message
(the route answers it as a plain 400, like the other accept refusals the page
recognises). Changing the sentence must keep the phrase, or change both.
"""

from __future__ import annotations

import re
from pathlib import Path

from services.invitation_service import UNVERIFIED_ADDRESS_ACCEPT_MSG

_PAGE = (
    Path(__file__).resolve().parents[3]
    / "frontend"
    / "src"
    / "app"
    / "invite"
    / "[token]"
    / "page.tsx"
)


def test_the_invite_page_marker_is_in_the_message() -> None:
    match = re.search(r'const UNVERIFIED_ADDRESS_MARKER = "([^"]+)";', _PAGE.read_text("utf-8"))
    assert match is not None
    assert match.group(1) in UNVERIFIED_ADDRESS_ACCEPT_MSG
