"""A setup credential must never become a generic owner billing session (#2878)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import OKPKey
from joserfc.jwt import JWTClaimsRegistry

from auth import sync_setup_handoff
from auth.billing_handoff import BillingHandoffInvalid, BillingHandoffSigner, verify_handoff_token
from auth.sync_setup_handoff import (
    SyncSetupHandoffExpired,
    SyncSetupHandoffSigner,
    SyncSetupHandoffUnavailable,
)

from ._billing_handoff_helpers import ed25519_keypair, token_header
from ._billing_handoff_helpers import handoff_settings as legacy_handoff_settings


def handoff_settings(
    signing_key, *, kid="setup-key", iss="kagura-memory-cloud", aud="kagura-billing", ttl=120
):
    legacy, _ = ed25519_keypair()
    settings = legacy_handoff_settings(legacy, kid="legacy-key", iss=iss, aud=aud, ttl=ttl)
    settings.billing_sync_setup_signing_key = signing_key
    settings.billing_sync_setup_key_id = kid
    return settings


CONTRACT = json.loads(
    (Path(__file__).parents[1] / "fixtures/sync-setup-handoff-v1.json").read_text()
)


@pytest.fixture
def context(monkeypatch):
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    monkeypatch.setattr(sync_setup_handoff, "utcnow", lambda: now)
    return {
        "user_id": "fixture-owner",
        "workspace_id": uuid4(),
        "service_id": "catalog-sync",
        "client_id": "public-web",
        "application_id": uuid4(),
        "application_revision": 7,
        "input_digest": "ab" * 32,
        "ownership_epoch": 19,
        "valid_until": now + timedelta(minutes=5),
    }


def verified(minted, public_pem):
    return jwt.decode(minted.token, OKPKey.import_key(public_pem), algorithms=["Ed25519"]).claims


def test_signed_setup_binds_original_context_without_generic_owner_role(context):
    private, public = ed25519_keypair()
    result = SyncSetupHandoffSigner(handoff_settings(private, kid="setup-key")).mint(**context)
    claims = verified(result, public)
    issued_at = int((context["valid_until"] - timedelta(minutes=5)).replace(tzinfo=UTC).timestamp())
    assert token_header(result.token) == {
        "alg": CONTRACT["token"]["algorithm"],
        "kid": "setup-key",
        "typ": CONTRACT["token"]["type"],
    }
    assert claims == {
        "iss": "kagura-memory-cloud",
        "aud": CONTRACT["token"]["audience"],
        "purpose": CONTRACT["token"]["purpose"],
        "sub": context["user_id"],
        "workspace_id": str(context["workspace_id"]),
        "service_id": context["service_id"],
        "client_id": context["client_id"],
        "application_id": str(context["application_id"]),
        "application_revision": context["application_revision"],
        "input_digest": context["input_digest"],
        "epoch": context["ownership_epoch"],
        "iat": issued_at,
        "exp": issued_at + CONTRACT["token"]["maximum_lifetime_seconds"],
        "jti": result.jti,
    }
    assert "role" not in claims
    assert set(claims) == set(CONTRACT["token"]["required_claims"])
    assert not set(claims).intersection(CONTRACT["token"]["forbidden_claims"])


def test_existing_generic_reference_verifier_cannot_redeem_setup(context):
    private, public = ed25519_keypair()
    result = SyncSetupHandoffSigner(handoff_settings(private)).mint(**context)
    JWTClaimsRegistry(
        iss={"essential": True, "value": "kagura-memory-cloud"},
        aud={"essential": True, "value": "kagura-billing-sync-setup"},
        iat={"essential": True},
        exp={"essential": True},
    ).validate(verified(result, public))
    with pytest.raises(BillingHandoffInvalid):
        verify_handoff_token(
            result.token,
            public,
            current_epoch=context["ownership_epoch"],
            issuer="kagura-memory-cloud",
            audience="kagura-billing",
        )


def test_never_mints_a_generic_token_as_an_intermediate(context, monkeypatch):
    private, public = ed25519_keypair()
    generic = Mock(side_effect=AssertionError("Generic billing authority must not be minted"))
    monkeypatch.setattr(BillingHandoffSigner, "mint", generic)
    result = SyncSetupHandoffSigner(handoff_settings(private)).mint(**context)
    assert verified(result, public)["purpose"] == "sync_addon_setup"
    generic.assert_not_called()


@pytest.mark.parametrize("remaining", [5, 9, 59])
def test_original_oidc_deadline_caps_handoff_without_renewal(context, remaining):
    private, public = ed25519_keypair()
    context["valid_until"] += timedelta(seconds=remaining, minutes=-5)
    result = SyncSetupHandoffSigner(handoff_settings(private)).mint(**context)
    claims = verified(result, public)
    assert claims["exp"] - claims["iat"] == remaining


@pytest.mark.parametrize("remaining", [4, 1, 0, -1])
def test_expired_original_authority_never_mints(context, remaining):
    private, _ = ed25519_keypair()
    context["valid_until"] += timedelta(seconds=remaining, minutes=-5)
    with pytest.raises(SyncSetupHandoffExpired) as failure:
        SyncSetupHandoffSigner(handoff_settings(private)).mint(**context)
    assert failure.value.status_code == 401


def test_every_handoff_has_its_own_single_use_identity(context):
    private, _ = ed25519_keypair()
    signer = SyncSetupHandoffSigner(handoff_settings(private))
    first, second = signer.mint(**context), signer.mint(**context)
    assert first.jti != second.jti
    assert str(UUID(first.jti)) == first.jti and str(UUID(second.jti)) == second.jti
    assert len(first.jti) >= 32 and len(second.jti) >= 32


def test_minimum_lifetime_uses_real_clock_after_integer_expiry_rounding(context, monkeypatch):
    private, _ = ed25519_keypair()
    now = context["valid_until"] - timedelta(minutes=5) + timedelta(microseconds=1)
    monkeypatch.setattr(sync_setup_handoff, "utcnow", lambda: now)
    context["valid_until"] = now + timedelta(seconds=5)
    with pytest.raises(SyncSetupHandoffExpired):
        SyncSetupHandoffSigner(handoff_settings(private)).mint(**context)
    context["valid_until"] = now + timedelta(seconds=6)
    assert SyncSetupHandoffSigner(handoff_settings(private)).mint(**context).expires_at > now


def test_configured_generic_audience_collision_is_refused(context):
    private, _ = ed25519_keypair()
    settings = handoff_settings(private, aud="kagura-billing-sync-setup")
    with pytest.raises(SyncSetupHandoffUnavailable):
        SyncSetupHandoffSigner(settings).mint(**context)


@pytest.mark.parametrize("kind", ["missing_key", "missing_kid", "invalid_key", "public_key"])
def test_signing_failures_reveal_no_material(context, monkeypatch, kind):
    private, public = ed25519_keypair()
    settings = handoff_settings(private)
    if kind == "missing_key":
        settings.billing_sync_setup_signing_key = ""
    elif kind == "missing_kid":
        settings.billing_sync_setup_key_id = ""
    elif kind == "invalid_key":
        settings.billing_sync_setup_signing_key = "SENSITIVE_SYNTHETIC_INVALID_KEY"
    else:
        settings.billing_sync_setup_signing_key = public
    diagnostic = Mock()
    monkeypatch.setattr(sync_setup_handoff, "logger", diagnostic)
    with pytest.raises(SyncSetupHandoffUnavailable) as failure:
        SyncSetupHandoffSigner(settings).mint(**context)
    assert "SENSITIVE_SYNTHETIC_INVALID_KEY" not in str(failure.value)
    assert context["user_id"] not in str(diagnostic.mock_calls)
    assert private not in str(diagnostic.mock_calls)
    assert public not in str(diagnostic.mock_calls)
    assert "SENSITIVE_SYNTHETIC_INVALID_KEY" not in str(diagnostic.mock_calls)


@pytest.mark.parametrize("collision", ["same_key", "same_key_crlf", "same_kid"])
def test_setup_key_and_identifier_cannot_alias_generic_billing(context, collision):
    private, _public = ed25519_keypair()
    settings = handoff_settings(private)
    if collision == "same_kid":
        settings.billing_sync_setup_key_id = settings.billing_handoff_key_id
    else:
        settings.billing_handoff_signing_key = (
            private if collision == "same_key" else private.replace("\n", "\r\n")
        )
    with pytest.raises(SyncSetupHandoffUnavailable):
        SyncSetupHandoffSigner(settings).mint(**context)


def test_setup_signature_is_not_verifiable_by_legacy_key(context):
    private, _public = ed25519_keypair()
    legacy, legacy_public = ed25519_keypair()
    settings = handoff_settings(private)
    settings.billing_handoff_signing_key = legacy
    result = SyncSetupHandoffSigner(settings).mint(**context)
    with pytest.raises(JoseError):
        jwt.decode(result.token, OKPKey.import_key(legacy_public), algorithms=["Ed25519"])


def test_prepared_signer_does_not_reparse_pem_during_mint(context, monkeypatch):
    private, public = ed25519_keypair()
    signer = SyncSetupHandoffSigner(handoff_settings(private))
    signer.validate_configuration()
    parser = Mock(side_effect=AssertionError("PEM parse after entering authority"))
    monkeypatch.setattr(sync_setup_handoff, "load_pem_private_key", parser)
    assert verified(signer.mint(**context), public)["sub"] == context["user_id"]
    parser.assert_not_called()


def test_mint_failure_logs_only_exception_type(context, monkeypatch):
    private, _public = ed25519_keypair()
    signer = SyncSetupHandoffSigner(handoff_settings(private))
    signer.validate_configuration()
    diagnostic = Mock()
    monkeypatch.setattr(sync_setup_handoff, "logger", diagnostic)
    monkeypatch.setattr(
        sync_setup_handoff.jwt, "encode", Mock(side_effect=RuntimeError("SENSITIVE_SIGNER_ERROR"))
    )
    with pytest.raises(SyncSetupHandoffUnavailable):
        signer.mint(**context)
    diagnostic.warning.assert_called_once_with(
        "sync_setup_handoff_unavailable", failure_type="RuntimeError"
    )
    assert "SENSITIVE_SIGNER_ERROR" not in str(diagnostic.mock_calls)
    assert private not in str(diagnostic.mock_calls)
