"""Unit tests for ``_sanitize_challenge_attr_value`` in mcp_server.transport.

Pins the header-value sanitizer added in #592. ``error_description`` lands
in the RFC 6750 ``WWW-Authenticate: Bearer ...`` quoted-attribute value,
and a stray ``"`` or CR/LF would close the attribute early or split the
HTTP response (CWE-93). The sanitizer must neutralize all three.
"""

import sys
from pathlib import Path

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

import pytest  # noqa: E402

from mcp_server.transport import _sanitize_challenge_attr_value  # noqa: E402


class TestSanitizeChallengeAttrValue:
    """Three characters must always be stripped: ``\\r``, ``\\n``, ``"``."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ('Bad token: "', "Bad token: '"),
            ("Bad token: \r\n", "Bad token:   "),
            ('Bad: "x"\r\nfake-header: y', "Bad: 'x'  fake-header: y"),
            ("plain message", "plain message"),
            ("", ""),
        ],
    )
    def test_strips_crlf_and_quotes(self, raw: str, expected: str) -> None:
        result = _sanitize_challenge_attr_value(raw)
        assert result == expected
        assert "\r" not in result
        assert "\n" not in result
        assert '"' not in result

    def test_attacker_payload_cannot_inject_fake_resource_metadata(self) -> None:
        """A poisoned error_description must not be able to inject a fake
        ``resource_metadata="https://evil.example/..."`` attribute by closing
        the legitimate ``error_description="..."`` attribute early.
        """
        payload = '"\r\nresource_metadata="https://evil.example/x"'
        result = _sanitize_challenge_attr_value(payload)
        # The closing quote that would have terminated error_description is
        # replaced by an apostrophe, and the CR/LF that would have started a
        # new header line is replaced by spaces. The payload becomes inert.
        assert '"' not in result
        assert "\r" not in result
        assert "\n" not in result
