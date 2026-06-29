"""MCP tool handlers for the zero-knowledge secret store (#1128).

Five tools mirroring the REST consumption/management surface:

- ``secret_register_pubkey`` (member) — register the caller's own age recipient
  pubkey (becomes pending owner approval; approval is a REST/owner-console action,
  deliberately NOT exposed on the LLM surface).
- ``secret_put`` (owner/admin) — store opaque ciphertext + grants.
- ``secret_get`` (member + grant) — fetch ciphertext. The value is opaque ``age``
  ciphertext the server cannot read; the agent decrypts it locally with its
  private key. Default-deny + fail-closed audit are enforced in the service.
- ``secret_list`` (owner/admin) — names + metadata only, never values.
- ``secret_revoke_grant`` (owner/admin) — revoke a grant + flag rotation.

Workspace gating happens here (role lookup); the value-level default-deny grant
check lives in :class:`SecretStoreService`. Write tools commit explicitly — the
``async for db in get_db()`` loop does not auto-commit on early return.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

from mcp.types import TextContent

from db.base import get_db
from mcp_server.tools._helpers import (
    _check_viewer_permission,
    _error_response,
    _get_workspace_member_role,
    _log_tool_usage,
    _success_response,
)
from services.secret_store_service import (
    SecretAccessDenied,
    SecretNotFound,
    SecretStoreService,
)
from utils.datetime import to_utc_iso

logger = logging.getLogger(__name__)


def _require_workspace(workspace_id: UUID | None) -> list[TextContent] | None:
    if workspace_id is None:
        return _error_response("workspace_required", "No workspace selected for this request.")
    return None


def _parse_uuid_list(raw: Any, field: str) -> list[UUID]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"'{field}' must be a non-empty array of pubkey ids")
    out: list[UUID] = []
    for x in raw:
        try:
            out.append(UUID(str(x)))
        except (ValueError, TypeError) as e:
            raise ValueError(f"'{field}' contains an invalid UUID: {x!r}") from e
    return out


async def handle_secret_register_pubkey(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Register the caller's own age recipient pubkey (pending owner approval)."""
    guard = _require_workspace(workspace_id)
    if guard:
        return guard
    pubkey = args.get("pubkey")
    if not isinstance(pubkey, str) or not pubkey:
        return _error_response("invalid_arguments", "'pubkey' (age recipient string) is required.")
    label = args.get("label")

    start_time = time.time()
    async for db in get_db():
        try:
            viewer = await _check_viewer_permission(
                db, user_id, workspace_id, "register a secret pubkey"
            )
            if viewer:
                return viewer
            svc = SecretStoreService(db)
            row = await svc.register_pubkey(
                workspace_id=workspace_id, actor_user_id=user_id, pubkey=pubkey, label=label
            )
            await db.commit()
            await _log_tool_usage(
                db, user_id, "secret_register_pubkey", start_time, 200, None, workspace_id
            )
            return _success_response(
                pubkey_id=str(row.id), fingerprint=row.fingerprint, status=row.status
            )
        except ValueError as e:
            await db.rollback()
            return _error_response("invalid_arguments", str(e))
        except Exception as e:
            await db.rollback()
            logger.error(f"secret_register_pubkey_failed: {e}", exc_info=True)
            await _log_tool_usage(
                db, user_id, "secret_register_pubkey", start_time, 500, None, workspace_id
            )
            return _error_response("secret_register_pubkey_error", "An internal error occurred.")
    return _error_response("internal_error", "Failed to acquire database session")


async def handle_secret_put(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Store a new ciphertext version + grants (owner/admin). Never sees plaintext."""
    guard = _require_workspace(workspace_id)
    if guard:
        return guard
    name = args.get("name")
    ciphertext = args.get("ciphertext")
    if not isinstance(name, str) or not name:
        return _error_response("invalid_arguments", "'name' is required.")
    if not isinstance(ciphertext, str) or not ciphertext:
        return _error_response(
            "invalid_arguments", "'ciphertext' (armored age ciphertext) is required."
        )
    recipients_snapshot = args.get("recipients_snapshot")
    if not isinstance(recipients_snapshot, list) or not recipients_snapshot:
        return _error_response(
            "invalid_arguments", "'recipients_snapshot' must be a non-empty array of fingerprints."
        )
    try:
        grant_ids = _parse_uuid_list(args.get("grant_pubkey_ids"), "grant_pubkey_ids")
    except ValueError as e:
        return _error_response("invalid_arguments", str(e))

    start_time = time.time()
    async for db in get_db():
        try:
            role = await _get_workspace_member_role(db, user_id, workspace_id)
            if role not in ("owner", "admin"):
                return _error_response(
                    "forbidden", "Storing a secret requires workspace owner or admin."
                )
            svc = SecretStoreService(db)
            secret, version = await svc.put_secret(
                workspace_id=workspace_id,
                actor_user_id=user_id,
                name=name,
                ciphertext=ciphertext,
                recipients_snapshot=[str(r) for r in recipients_snapshot],
                grant_pubkey_ids=grant_ids,
            )
            await db.commit()
            await _log_tool_usage(db, user_id, "secret_put", start_time, 200, None, workspace_id)
            return _success_response(
                name=secret.name,
                version_number=version.version_number,
                status=secret.status,
                rotation_needed=secret.rotation_needed,
            )
        except ValueError as e:
            await db.rollback()
            return _error_response("invalid_arguments", str(e))
        except Exception as e:
            await db.rollback()
            logger.error(f"secret_put_failed: {e}", exc_info=True)
            await _log_tool_usage(db, user_id, "secret_put", start_time, 500, None, workspace_id)
            return _error_response("secret_put_error", "An internal error occurred.")
    return _error_response("internal_error", "Failed to acquire database session")


async def handle_secret_get(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Fetch a secret's opaque ciphertext (member + grant; default-deny, audit-first).

    The returned ciphertext is decryptable only by the caller's age private key,
    which never leaves the client; the server holds no key for it.
    """
    guard = _require_workspace(workspace_id)
    if guard:
        return guard
    name = args.get("name")
    if not isinstance(name, str) or not name:
        return _error_response("invalid_arguments", "'name' is required.")
    version_number = args.get("version_number")
    if version_number is not None and (
        isinstance(version_number, bool) or not isinstance(version_number, int)
    ):
        return _error_response("invalid_arguments", "'version_number' must be an integer.")

    start_time = time.time()
    async for db in get_db():
        try:
            viewer = await _check_viewer_permission(db, user_id, workspace_id, "fetch a secret")
            if viewer:
                return viewer
            svc = SecretStoreService(db)
            # get_secret commits its own fail-closed audit entry internally.
            payload = await svc.get_secret(
                workspace_id=workspace_id,
                actor_user_id=user_id,
                name=name,
                version_number=version_number,
            )
            await _log_tool_usage(db, user_id, "secret_get", start_time, 200, None, workspace_id)
            return _success_response(
                name=payload["name"],
                version_number=payload["version_number"],
                alg=payload["alg"],
                ciphertext=payload["ciphertext"],
                recipients_snapshot=payload["recipients_snapshot"],
                rotation_needed=payload["rotation_needed"],
            )
        except SecretAccessDenied:
            await _log_tool_usage(db, user_id, "secret_get", start_time, 403, None, workspace_id)
            return _error_response("access_denied", "Access denied.")
        except SecretNotFound:
            await _log_tool_usage(db, user_id, "secret_get", start_time, 404, None, workspace_id)
            return _error_response("not_found", "Secret version not found.")
        except Exception as e:
            await db.rollback()
            logger.error(f"secret_get_failed: {e}", exc_info=True)
            await _log_tool_usage(db, user_id, "secret_get", start_time, 500, None, workspace_id)
            return _error_response("secret_get_error", "An internal error occurred.")
    return _error_response("internal_error", "Failed to acquire database session")


async def handle_secret_list(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """List secret names + metadata (owner/admin). Never returns values."""
    guard = _require_workspace(workspace_id)
    if guard:
        return guard

    start_time = time.time()
    async for db in get_db():
        try:
            role = await _get_workspace_member_role(db, user_id, workspace_id)
            if role not in ("owner", "admin"):
                return _error_response(
                    "forbidden", "Listing secrets requires workspace owner or admin."
                )
            svc = SecretStoreService(db)
            rows = await svc.list_secrets(workspace_id=workspace_id)
            secrets = [
                {
                    "name": r["name"],
                    "status": r["status"],
                    "rotation_needed": r["rotation_needed"],
                    "current_version": r["current_version"],
                    "grant_count": r["grant_count"],
                    "created_at": to_utc_iso(r["created_at"]),
                    "updated_at": to_utc_iso(r["updated_at"]),
                }
                for r in rows
            ]
            await _log_tool_usage(db, user_id, "secret_list", start_time, 200, None, workspace_id)
            return _success_response(secrets=secrets, count=len(secrets))
        except Exception as e:
            await db.rollback()
            logger.error(f"secret_list_failed: {e}", exc_info=True)
            await _log_tool_usage(db, user_id, "secret_list", start_time, 500, None, workspace_id)
            return _error_response("secret_list_error", "An internal error occurred.")
    return _error_response("internal_error", "Failed to acquire database session")


async def handle_secret_revoke_grant(
    args: dict[str, Any], user_id: str, workspace_id: UUID | None
) -> list[TextContent]:
    """Revoke one recipient's grant on a secret + flag rotation (owner/admin)."""
    guard = _require_workspace(workspace_id)
    if guard:
        return guard
    name = args.get("name")
    if not isinstance(name, str) or not name:
        return _error_response("invalid_arguments", "'name' is required.")
    raw_pid = args.get("recipient_pubkey_id")
    try:
        pubkey_id = UUID(str(raw_pid))
    except (ValueError, TypeError):
        return _error_response("invalid_arguments", "'recipient_pubkey_id' must be a valid UUID.")

    start_time = time.time()
    async for db in get_db():
        try:
            role = await _get_workspace_member_role(db, user_id, workspace_id)
            if role not in ("owner", "admin"):
                return _error_response(
                    "forbidden", "Revoking a grant requires workspace owner or admin."
                )
            svc = SecretStoreService(db)
            secret = await svc.revoke_grant(
                workspace_id=workspace_id,
                actor_user_id=user_id,
                name=name,
                recipient_pubkey_id=pubkey_id,
            )
            await db.commit()
            await _log_tool_usage(
                db, user_id, "secret_revoke_grant", start_time, 200, None, workspace_id
            )
            return _success_response(name=secret.name, rotation_needed=secret.rotation_needed)
        except SecretNotFound as e:
            await db.rollback()
            return _error_response("not_found", str(e))
        except Exception as e:
            await db.rollback()
            logger.error(f"secret_revoke_grant_failed: {e}", exc_info=True)
            await _log_tool_usage(
                db, user_id, "secret_revoke_grant", start_time, 500, None, workspace_id
            )
            return _error_response("secret_revoke_grant_error", "An internal error occurred.")
    return _error_response("internal_error", "Failed to acquire database session")
