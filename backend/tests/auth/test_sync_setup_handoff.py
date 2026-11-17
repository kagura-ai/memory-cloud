"""A setup credential must never become a generic owner billing session (#2878)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from joserfc import jwt
from joserfc.jwk import OKPKey
from joserfc.jwt import JWTClaimsRegistry

from auth import sync_setup_handoff
from auth.billing_handoff import BillingHandoffInvalid, BillingHandoffSigner, verify_handoff_token
from auth.sync_setup_handoff import SyncSetupHandoffSigner, SyncSetupHandoffUnavailable

from ._billing_handoff_helpers import ed25519_keypair, handoff_settings, token_header

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


@pytest.mark.parametrize("remaining", [1, 9, 59])
def test_original_oidc_deadline_caps_handoff_without_renewal(context, remaining):
    private, public = ed25519_keypair()
    context["valid_until"] += timedelta(seconds=remaining, minutes=-5)
    result = SyncSetupHandoffSigner(handoff_settings(private)).mint(**context)
    claims = verified(result, public)
    assert claims["exp"] - claims["iat"] == remaining


@pytest.mark.parametrize("remaining", [0, -1])
def test_expired_original_authority_never_mints(context, remaining):
    private, _ = ed25519_keypair()
    context["valid_until"] += timedelta(seconds=remaining, minutes=-5)
    with pytest.raises(SyncSetupHandoffUnavailable):
        SyncSetupHandoffSigner(handoff_settings(private)).mint(**context)


def test_every_handoff_has_its_own_single_use_identity(context):
    private, _ = ed25519_keypair()
    signer = SyncSetupHandoffSigner(handoff_settings(private))
    first, second = signer.mint(**context), signer.mint(**context)
    assert first.jti != second.jti
    assert len(first.jti) >= 32 and len(second.jti) >= 32


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
        settings.billing_handoff_signing_key = ""
    elif kind == "missing_kid":
        settings.billing_handoff_key_id = ""
    elif kind == "invalid_key":
        settings.billing_handoff_signing_key = "SENSITIVE_SYNTHETIC_INVALID_KEY"
    else:
        settings.billing_handoff_signing_key = public
    diagnostic = Mock()
    monkeypatch.setattr(sync_setup_handoff, "logger", diagnostic)
    with pytest.raises(SyncSetupHandoffUnavailable) as failure:
        SyncSetupHandoffSigner(settings).mint(**context)
    assert "SENSITIVE_SYNTHETIC_INVALID_KEY" not in str(failure.value)
    assert context["user_id"] not in str(diagnostic.mock_calls)
    assert private not in str(diagnostic.mock_calls)
    assert public not in str(diagnostic.mock_calls)
    assert "SENSITIVE_SYNTHETIC_INVALID_KEY" not in str(diagnostic.mock_calls)
