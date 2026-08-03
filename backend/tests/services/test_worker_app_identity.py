"""Lifecycle tests for worker app secret rotation (#1315)."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from models.worker_app import WorkerAppIdentity
from services.worker_app_identity import WorkerAppIdentityService, identity_collection_revision
from utils.datetime import utcnow


@pytest.fixture
def _fernet_env(monkeypatch):
    monkeypatch.setenv("API_KEY_SECRET", Fernet.generate_key().decode())
    import utils.encryption as enc_module

    enc_module._encryptor = None
    yield
    enc_module._encryptor = None


@pytest.mark.asyncio
async def test_rotate_keeps_previous_secret_for_bounded_window(_fernet_env):
    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="active",
        active_secret_revision=4,
        config_version=8,
    )
    identity.set_active_signing_secret("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    before = utcnow()
    result = await service.rotate_secret(
        platform="slack",
        app_key="sales",
        signing_secret="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        retiring_for_seconds=3600,
        actor_id="admin-1",
    )

    assert result.get_active_signing_secret() == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert result.active_secret_revision == 5
    assert result.get_retiring_signing_secret() == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert result.retiring_secret_revision == 4
    assert before + timedelta(seconds=3599) <= result.retiring_valid_until
    assert result.config_version == 9
    assert result.status == "active"


@pytest.mark.asyncio
async def test_disable_preserves_ciphertext_but_bumps_revision(_fernet_env):
    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="active",
        active_secret_revision=1,
        config_version=1,
    )
    identity.set_active_signing_secret("cccccccccccccccccccccccccccccccc")
    ciphertext = identity.active_signing_secret_encrypted
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    result = await service.update_identity(
        platform="slack",
        app_key="sales",
        actor_id="admin-1",
        status="disabled",
    )

    assert result.status == "disabled"
    assert result.config_version == 2
    assert result.active_signing_secret_encrypted == ciphertext


@pytest.mark.asyncio
async def test_rotate_disabled_identity_stays_disabled_and_drops_revoked_secret(_fernet_env):
    """Revocation is sticky: rotating a disabled identity stages the new
    secret but must NOT resurrect the identity to active, and must NOT arm
    the (revoked) previous secret as a retiring revision that /workers/apps
    would re-serve to the fleet after a later re-enable."""
    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="disabled",
        active_secret_revision=3,
        config_version=5,
    )
    identity.set_active_signing_secret("dddddddddddddddddddddddddddddddd")
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    result = await service.rotate_secret(
        platform="slack",
        app_key="sales",
        signing_secret="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        retiring_for_seconds=3600,
        actor_id="admin-1",
    )

    assert result.status == "disabled"
    assert result.get_active_signing_secret() == "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    assert result.active_secret_revision == 4
    assert result.retiring_signing_secret_encrypted is None
    assert result.retiring_secret_revision is None
    assert result.retiring_valid_until is None
    assert result.config_version == 6


@pytest.mark.asyncio
async def test_rotate_unconfigured_identity_preserves_status(_fernet_env):
    """Staging a secret into the migration-window 'unconfigured' default
    must not flip it to active — that would switch config dispatch from the
    worker-env path to identity-governed before the operator opts in
    (enable stays the explicit update_identity(status='active') step)."""
    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="default",
        display_name="Default",
        status="unconfigured",
        config_version=1,
    )
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    result = await service.rotate_secret(
        platform="slack",
        app_key="default",
        signing_secret="ffffffffffffffffffffffffffffffff",
        retiring_for_seconds=3600,
        actor_id="admin-1",
    )

    assert result.status == "unconfigured"
    assert result.get_active_signing_secret() == "ffffffffffffffffffffffffffffffff"
    assert result.active_secret_revision == 1
    assert result.retiring_signing_secret_encrypted is None
    assert result.retiring_valid_until is None


@pytest.mark.asyncio
async def test_rotate_recovers_when_previous_ciphertext_undecryptable(_fernet_env):
    """#1356: rotate is the documented recovery path after a Fernet key
    rotation or ciphertext corruption — it must not 500 on the stored
    ciphertext. The unrecoverable previous secret is dropped (no retiring
    window) and the new secret is stored; that write IS the recovery."""
    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="active",
        active_secret_revision=4,
        config_version=8,
    )
    # Stored ciphertext that the current API_KEY_SECRET cannot decrypt.
    identity.active_signing_secret_encrypted = "gAAAAA-not-decryptable-with-current-key"
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    with patch("services.worker_app_identity.logger") as mock_logger:
        result = await service.rotate_secret(
            platform="slack",
            app_key="sales",
            signing_secret="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            retiring_for_seconds=3600,
            actor_id="admin-1",
        )

    assert result.get_active_signing_secret() == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert result.active_secret_revision == 5
    assert result.retiring_signing_secret_encrypted is None
    assert result.retiring_secret_revision is None
    assert result.retiring_valid_until is None
    assert result.config_version == 9
    assert result.status == "active"
    # Audit: the drop is recorded, ids/enums only — never secret material.
    assert mock_logger.warning.call_count == 1
    kwargs = mock_logger.warning.call_args.kwargs
    assert mock_logger.warning.call_args.args[0] == "worker_app_previous_secret_undecryptable"
    assert kwargs["platform"] == "slack"
    assert kwargs["app_key"] == "sales"
    for value in kwargs.values():
        assert "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" not in str(value)
        assert "gAAAAA" not in str(value)


@pytest.mark.asyncio
async def test_disable_clears_retiring_window(_fernet_env):
    """#1356: disabling revokes ALL secret material acceptance — the armed
    retiring secret must not survive to be revived by a later re-enable."""
    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="active",
        active_secret_revision=5,
        config_version=9,
    )
    identity.set_active_signing_secret("00000000000000000000000000000000")
    identity.set_retiring_signing_secret("previous")
    identity.retiring_secret_revision = 4
    identity.retiring_valid_until = utcnow() + timedelta(hours=1)
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    result = await service.update_identity(
        platform="slack",
        app_key="sales",
        actor_id="admin-1",
        status="disabled",
    )

    assert result.status == "disabled"
    assert result.retiring_signing_secret_encrypted is None
    assert result.retiring_secret_revision is None
    assert result.retiring_valid_until is None


@pytest.mark.asyncio
async def test_reenable_clears_stale_retiring_window(_fernet_env):
    """#1356: rows disabled before this fix may still carry retiring
    material — the enable transition must purge it so the old secret does
    not resurface in the worker bootstrap."""
    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="disabled",
        active_secret_revision=5,
        config_version=9,
    )
    identity.set_active_signing_secret("00000000000000000000000000000000")
    identity.set_retiring_signing_secret("revoked-old")
    identity.retiring_secret_revision = 4
    identity.retiring_valid_until = utcnow() + timedelta(hours=1)
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    result = await service.update_identity(
        platform="slack",
        app_key="sales",
        actor_id="admin-1",
        status="active",
    )

    assert result.status == "active"
    assert result.retiring_signing_secret_encrypted is None
    assert result.retiring_secret_revision is None
    assert result.retiring_valid_until is None


@pytest.mark.asyncio
async def test_rotate_undecryptable_on_disabled_identity_stays_disabled(_fernet_env):
    """Composition of the two #1356 behaviors: decrypt-failure recovery must
    not bypass sticky revocation — rotate on a disabled identity with
    rotated-away ciphertext succeeds, stays disabled, arms nothing."""
    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="disabled",
        active_secret_revision=3,
        config_version=5,
    )
    identity.active_signing_secret_encrypted = "gAAAAA-not-decryptable-with-current-key"
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    result = await service.rotate_secret(
        platform="slack",
        app_key="sales",
        signing_secret="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        retiring_for_seconds=3600,
        actor_id="admin-1",
    )

    assert result.status == "disabled"
    assert result.get_active_signing_secret() == "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    assert result.active_secret_revision == 4
    assert result.retiring_signing_secret_encrypted is None
    assert result.retiring_valid_until is None


@pytest.mark.asyncio
async def test_enable_rejects_undecryptable_ciphertext(_fernet_env):
    """#1356: enabling must not 200 when the fleet would be served nothing —
    an undecryptable stored secret fails loudly, pointing at rotate."""
    from utils.exceptions import ValidationError

    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="disabled",
        active_secret_revision=3,
        config_version=5,
    )
    identity.active_signing_secret_encrypted = "gAAAAA-not-decryptable-with-current-key"
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    with pytest.raises(ValidationError):
        await service.update_identity(
            platform="slack",
            app_key="sales",
            actor_id="admin-1",
            status="active",
        )


@pytest.mark.asyncio
async def test_disabled_noop_update_purges_stale_retiring_material(_fernet_env):
    """#1356: PATCH status=disabled on an already-disabled legacy row is the
    operator lever to scrub revoked retiring material at rest."""
    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="disabled",
        active_secret_revision=5,
        config_version=9,
    )
    identity.set_active_signing_secret("00000000000000000000000000000000")
    identity.set_retiring_signing_secret("revoked-old")
    identity.retiring_secret_revision = 4
    identity.retiring_valid_until = utcnow() + timedelta(hours=1)
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    with patch("services.worker_app_identity.logger") as mock_logger:
        result = await service.update_identity(
            platform="slack",
            app_key="sales",
            actor_id="admin-1",
            status="disabled",
        )

    assert result.status == "disabled"
    assert result.retiring_signing_secret_encrypted is None
    assert result.retiring_secret_revision is None
    assert result.retiring_valid_until is None
    # #1343 correlation: the teardown audit carries the PRE-clear revision.
    cleared = [
        c
        for c in mock_logger.info.call_args_list
        if c.args and c.args[0] == "worker_app_retiring_window_cleared"
    ]
    assert len(cleared) == 1
    assert cleared[0].kwargs["retiring_secret_revision"] == 4
    for value in cleared[0].kwargs.values():
        assert "revoked-old" not in str(value)
        assert "00000000000000000000000000000000" not in str(value)


@pytest.mark.asyncio
async def test_status_noop_update_keeps_retiring_window(_fernet_env):
    """A same-status PATCH (e.g. display_name edit sent with status=active)
    is not a transition — it must not tear down a legitimately armed
    rotation window mid-migration."""
    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="active",
        active_secret_revision=5,
        config_version=9,
    )
    identity.set_active_signing_secret("00000000000000000000000000000000")
    identity.set_retiring_signing_secret("previous")
    identity.retiring_secret_revision = 4
    valid_until = utcnow() + timedelta(hours=1)
    identity.retiring_valid_until = valid_until
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    result = await service.update_identity(
        platform="slack",
        app_key="sales",
        actor_id="admin-1",
        status="active",
        display_name="Sales EMEA",
    )

    assert result.status == "active"
    assert result.display_name == "Sales EMEA"
    assert result.get_retiring_signing_secret() == "previous"
    assert result.retiring_secret_revision == 4
    assert result.retiring_valid_until == valid_until


@pytest.mark.asyncio
async def test_rename_only_does_not_bump_config_version(_fernet_env):
    """#1360: display_name is admin-display-only — a rename must not bump
    config_version (the bump flips identity_collection_revision and makes
    every worker refetch its config on the next bootstrap poll)."""
    from services.worker_app_identity import identity_revision

    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        id=uuid4(),
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="active",
        active_secret_revision=2,
        config_version=7,
    )
    identity.set_active_signing_secret("cccccccccccccccccccccccccccccccc")
    revision_before = identity_revision(identity)
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    result = await service.update_identity(
        platform="slack",
        app_key="sales",
        actor_id="admin-1",
        display_name="Sales EMEA",
    )

    assert result.display_name == "Sales EMEA"
    assert result.config_version == 7
    assert identity_revision(result) == revision_before
    assert result.updated_by == "admin-1"


@pytest.mark.asyncio
async def test_status_transition_still_bumps_config_version(_fernet_env):
    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="active",
        active_secret_revision=2,
        config_version=7,
    )
    identity.set_active_signing_secret("cccccccccccccccccccccccccccccccc")
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    result = await service.update_identity(
        platform="slack",
        app_key="sales",
        actor_id="admin-1",
        status="disabled",
    )

    assert result.status == "disabled"
    assert result.config_version == 8


def test_bootstrap_revision_changes_when_retiring_window_expires():
    identity = WorkerAppIdentity(
        id=uuid4(),
        platform="slack",
        app_key="sales",
        display_name="Sales",
        status="active",
        config_version=2,
        retiring_valid_until=utcnow() + timedelta(minutes=5),
    )
    before_expiry = identity_collection_revision([identity])
    identity.retiring_valid_until = utcnow() - timedelta(seconds=1)

    assert identity_collection_revision([identity]) != before_expiry


# ── #1478: signing-secret shape validation ───────────────────────────


@pytest.mark.parametrize(
    "bad_secret",
    [
        pytest.param("ここに新しい署名シークレット", id="the-placeholder-that-caused-the-outage"),
        pytest.param("PASTE_YOUR_SIGNING_SECRET_HERE", id="ascii-placeholder"),
        pytest.param("A" * 32, id="uppercase-hex"),
        pytest.param("a" * 31, id="one-char-short"),
        pytest.param("a" * 33, id="one-char-long"),
        pytest.param("g" * 32, id="right-length-not-hex"),
        pytest.param("a" * 16 + " " + "a" * 15, id="embedded-space"),
        pytest.param("a" * 32 + "\n", id="trailing-newline"),
    ],
)
@pytest.mark.asyncio
async def test_rotate_rejects_a_secret_that_is_not_slack_shaped(_fernet_env, bad_secret):
    """#1478: `min_length=1` let a placeholder through, it was stored as ACTIVE,
    and every webhook then failed verification.

    The first case is the exact string that caused the production outage.
    """
    from utils.exceptions import ValidationError

    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="default",
        display_name="Slack App",
        status="active",
        active_secret_revision=1,
        config_version=1,
    )
    identity.set_active_signing_secret("a" * 32)
    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    with pytest.raises(ValidationError):
        await service.rotate_secret(
            platform="slack",
            app_key="default",
            signing_secret=bad_secret,
            retiring_for_seconds=900,
            actor_id="admin-1",
        )


@pytest.mark.asyncio
async def test_a_rejected_rotation_leaves_the_working_secret_untouched(_fernet_env):
    """The outage was made worse because the bad value DISPLACED the working
    one: active became the placeholder and the real secret went to retiring,
    which then expired. A rejected rotation must be a no-op on stored state."""
    from utils.exceptions import ValidationError

    db = MagicMock()
    db.flush = AsyncMock()
    identity = WorkerAppIdentity(
        platform="slack",
        app_key="default",
        display_name="Slack App",
        status="active",
        active_secret_revision=7,
        config_version=3,
    )
    identity.set_active_signing_secret("a" * 32)
    before_active = identity.active_signing_secret_encrypted
    before_revision = identity.active_secret_revision
    before_version = identity.config_version

    service = WorkerAppIdentityService(db)
    service._require_identity = AsyncMock(return_value=identity)

    with pytest.raises(ValidationError):
        await service.rotate_secret(
            platform="slack",
            app_key="default",
            signing_secret="not-a-secret",
            retiring_for_seconds=900,
            actor_id="admin-1",
        )

    assert identity.active_signing_secret_encrypted == before_active
    assert identity.active_secret_revision == before_revision
    assert identity.config_version == before_version
    assert identity.retiring_signing_secret_encrypted is None
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_validation_error_never_echoes_the_submitted_secret(_fernet_env):
    """An admin who pastes the wrong thing must not get it reflected into a
    response body or a log line — the wrong thing is very often the right
    secret for somewhere else."""
    from services.worker_app_identity import validate_signing_secret
    from utils.exceptions import ValidationError

    leaked = "hunter2-this-is-some-other-systems-credential"
    with pytest.raises(ValidationError) as exc:
        validate_signing_secret("slack", leaked)

    assert leaked not in str(exc.value)
    assert str(len(leaked)) in str(exc.value)  # length IS reported


@pytest.mark.asyncio
async def test_unknown_platform_keeps_todays_permissive_bounds(_fernet_env):
    """Adding Discord/Teams must be additive. A platform whose format has not
    been described yet must not have a Slack shape guessed for it — that would
    reject a valid credential."""
    from services.worker_app_identity import validate_signing_secret

    validate_signing_secret("discord", "a-format-nobody-has-described-yet")


@pytest.mark.asyncio
async def test_create_identity_also_validates(_fernet_env):
    """Both write paths funnel through the service; rotate is not the only door."""
    from utils.exceptions import ValidationError

    db = MagicMock()
    db.flush = AsyncMock()
    service = WorkerAppIdentityService(db)
    service.get_identity = AsyncMock(return_value=None)

    with pytest.raises(ValidationError):
        await service.create_identity(
            platform="slack",
            app_key="default",
            display_name="Slack App",
            signing_secret="ここに新しい署名シークレット",
            actor_id="admin-1",
        )
