"""SYSTEM_ADMIN_MODE service pieces (#2767): operator address, principal, credential.

The mode helpers and the first-account role are pinned in
``test_system_admin_mode_seam.py``.
"""

import ast
import secrets
from pathlib import Path

import pytest

from auth import system_admin_mode as sam
from config.settings import get_settings
from utils.exceptions import (
    OperatorEmailInvalidError,
    SystemAdminCredentialError,
    SystemAdminNotConfiguredError,
)

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


class TestNormalizeOperatorEmail:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ops@example.com", "ops@example.com"),
            ("  Ops.Team+on-call@Example.COM \t", "ops.team+on-call@example.com"),
            ("a_b-c@sub.example.co.jp", "a_b-c@sub.example.co.jp"),
            ("x@ex.io", "x@ex.io"),
        ],
    )
    def test_valid_addresses_are_trimmed_and_lower_cased(self, raw, expected):
        assert sam.normalize_operator_email([raw]) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "ops",
            "ops@",
            "@example.com",
            "ops@@example.com",
            "ops@local",  # no dot in the domain
            "ops@127.0.0.1",
            "ops@[127.0.0.1]",
            "ops@-example.com",
            "ops@example.c",
            ".ops@example.com",
            "ops.@example.com",
            "o..ps@example.com",
            "o'ps@example.com",
            "o`ps@example.com",
            "o{p}s@example.com",
            "o|ps@example.com",
            "o!ps@example.com",
            "o#ps@example.com",
            "o$ps@example.com",
            "o%ps@example.com",
            "o&ps@example.com",
            "o*ps@example.com",
            "o/ps@example.com",
            "o=ps@example.com",
            "o?ps@example.com",
            "o^ps@example.com",
            "o~ps@example.com",
            "o ps@example.com",
            "ops@exa mple.com",
            "ops@example.com\r\nX-Injected: 1",
            "ops@example.com,other@example.com",
            "ops;x@example.com",
            '"ops"@example.com',
            "<ops@example.com>",
            "opé@example.com",
            "ops@exämple.com",
            "Kops@example.com",  # Kelvin sign: lower() would make it ASCII "k"
            "a" * 65 + "@example.com",
        ],
    )
    def test_invalid_addresses_are_refused(self, raw):
        with pytest.raises(OperatorEmailInvalidError) as info:
            sam.normalize_operator_email([raw])
        assert info.value.error_code == "SYSADMIN-003"
        assert info.value.status_code == 400
        # The value is never echoed.
        assert raw.strip() == "" or raw.strip().lower() not in info.value.message.lower()

    def test_length_limit_keeps_the_principal_inside_string_255(self):
        assert sam.OPERATOR_EMAIL_MAX_LEN == 246

        def address(total: int) -> str:
            local = "a" * 64
            tail = total - len(local) - 1 - (60 + 1 + 60 + 1) - len(".com")
            return f"{local}@{'c' * 60}.{'d' * 60}.{'e' * tail}.com"

        at_limit = address(246)
        assert len(at_limit) == 246
        assert sam.normalize_operator_email([at_limit]) == at_limit
        assert len(sam.OPERATOR_PRINCIPAL_PREFIX + at_limit) == 255
        too_long = address(247)
        assert len(too_long) == 247
        with pytest.raises(OperatorEmailInvalidError):
            sam.normalize_operator_email([too_long])

    @pytest.mark.parametrize(
        "values", [[], ["a@example.com", "a@example.com"], ["a@example.com", "b@example.com"]]
    )
    def test_exactly_one_header_value(self, values):
        with pytest.raises(OperatorEmailInvalidError):
            sam.normalize_operator_email(values)


class TestOperatorPrincipal:
    def test_shape(self):
        principal = sam.build_operator_principal("ops@example.com")
        assert principal == {
            "user_id": "operator:ops@example.com",
            "sub": "operator:ops@example.com",
            "email": "operator:ops@example.com",
            "name": "operator:ops@example.com",
            "role": "operator",
            "principal_kind": "operator",
            "operator_email": "ops@example.com",
            "current_workspace_id": None,
        }
        assert "id" not in principal
        assert sam.is_operator_principal(principal)
        # A forgotten role check fails closed in every mode.
        assert not sam.role_grants_system_admin(principal["role"])

    def test_get_user_id_resolves_the_operator_id(self):
        from utils import get_user_id

        assert (
            get_user_id(sam.build_operator_principal("ops@example.com"))
            == "operator:ops@example.com"
        )

    @pytest.mark.parametrize(
        ("actor", "expected"),
        [
            ("operator:Ops@Example.com", "ops@example.com"),
            ("ops@example.com", "ops@example.com"),
            (" OPS@example.com ", "ops@example.com"),
            ("", None),
            (None, None),
            ("operator:", None),
        ],
    )
    def test_operator_address(self, actor, expected):
        assert sam.operator_address(actor) == expected


class TestReservedUserIdPrefixes:
    # Every users.user_id format minted today. A new format must not start
    # with a reserved prefix, or an account could share an operator's id.
    KNOWN_FORMATS = (
        "108123456789012345678",
        "12345678",
        "local:admin",
        "public:abc123",
        "email:3b0b2f5e-0000-4000-8000-000000000000",
    )

    def test_known_formats_never_use_a_reserved_prefix(self):
        for user_id in self.KNOWN_FORMATS:
            assert not user_id.startswith(sam.RESERVED_USER_ID_PREFIXES), user_id

    def test_operator_prefix_is_reserved(self):
        assert sam.build_operator_principal("a@example.com")["user_id"].startswith(
            sam.RESERVED_USER_ID_PREFIXES
        )

    def test_no_source_file_mints_an_operator_id_outside_the_mode_module(self):
        # A string literal starting with "operator:" anywhere else in backend/src
        # would be a second place that builds (or fakes) an operator id.
        offenders = []
        for path in sorted(SRC_ROOT.rglob("*.py")):
            if path.relative_to(SRC_ROOT).as_posix() == "auth/system_admin_mode.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value.startswith("operator:")
                ):
                    offenders.append(f"{path.relative_to(SRC_ROOT)}:{node.lineno}")
        assert offenders == []


class TestVerifyServiceCredential:
    @pytest.fixture
    def creds(self, monkeypatch):
        current = secrets.token_hex(32)
        previous = secrets.token_hex(32)
        settings = get_settings()
        monkeypatch.setattr(settings, "system_admin_service_token", current)
        monkeypatch.setattr(settings, "system_admin_service_token_previous", previous)
        return current, previous

    def test_unset_credential_is_503(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "system_admin_service_token", "")
        monkeypatch.setattr(get_settings(), "system_admin_service_token_previous", "x" * 40)
        with pytest.raises(SystemAdminNotConfiguredError) as info:
            sam.verify_service_credential("Bearer " + "x" * 40)
        assert info.value.status_code == 503
        assert info.value.error_code == "SYSADMIN-001"

    def test_current_and_previous_slots(self, creds):
        current, previous = creds
        assert sam.verify_service_credential(f"Bearer {current}") == 0
        assert sam.verify_service_credential(f"Bearer {previous}") == 1

    @pytest.mark.parametrize(
        "header",
        [
            None,
            "",
            "Bearer",
            "Bearer ",
            "bearer {current}",
            "BEARER {current}",
            "Basic {current}",
            "Token {current}",
            "Bearer  {current}",
            "Bearer {current} ",
            "Bearer {current}\t",
            "Bearer {current}x",
            "Bearer wrong",
            "Bearer ééé",  # latin-1 decoded header bytes >= 0x80
            "Bearer ☃",
            "Bearer \udcff",  # lone surrogate must not raise
        ],
    )
    def test_missing_malformed_or_wrong_is_401_never_500(self, creds, header):
        current, _ = creds
        value = header.format(current=current) if header else header
        with pytest.raises(SystemAdminCredentialError) as info:
            sam.verify_service_credential(value)
        assert info.value.status_code == 401
        assert info.value.error_code == "SYSADMIN-002"
        assert info.value.response_headers == {"WWW-Authenticate": "Bearer"}

    def test_every_candidate_is_compared(self, creds, monkeypatch):
        # No short-circuit: a match on the current credential still compares
        # the previous one, so timing does not tell which slot matched.
        current, _ = creds
        calls = []
        real = sam.hmac.compare_digest

        def counting(a, b):
            calls.append(1)
            return real(a, b)

        monkeypatch.setattr(sam.hmac, "compare_digest", counting)
        sam.verify_service_credential(f"Bearer {current}")
        assert len(calls) == 2
        calls.clear()
        with pytest.raises(SystemAdminCredentialError):
            sam.verify_service_credential("Bearer nope")
        assert len(calls) == 2
