"""Account routes for the workspace slots of the signed-in owner (Issue #2918).

Routes under ``/api/v1/me/workspace-slots``. An event that ends a paid
contract, lowers a plan's owned-workspace grant or lowers the slot bonus, and
does not raise the allowance, suspends the owner's excess owned workspaces
with their data kept (``services.workspace_slots``). Here the owner
reads their allowance and the status of every workspace they own, previews and
saves which workspaces to keep active, and lists the contexts of an owned
workspace they can export, also while it is suspended.

The surface is **experimental**: it may change in a minor release.

- Browser session only (``SessionUser``): an API key or an OAuth access token
  gets the existing 403.
- Served only while ``WORKSPACE_SLOT_SUSPENSION=enforce``. In ``off`` and
  ``shadow`` every route of this surface answers 404 ``HTTP-404`` "Not Found"
  (the body of an unknown path), from a router-level dependency evaluated
  before authentication, and no statement runs (a malformed path id gets the
  same 404). A method a route does not serve (405), the trailing-slash
  redirect (307) and a body that is not JSON (422, read before any
  dependency) come from the framework as on any other path.
- Ownership is ``Workspace.owner_user_id``; membership in somebody else's
  workspace takes no slot and is not listed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import SessionUser, require_workspace_slots_enabled
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.auth import Context, Workspace
from services.identity_link_service import owned_by
from services.workspace_slots import (
    SelectionPreview,
    SlotOverview,
    get_slot_overview,
    preview_selection,
    save_selection,
)
from utils.exceptions import NotFoundException

# Upper bound of one selection list. Far above any allowance a plan grants;
# it only bounds the request size (the service validates every id).
MAX_SELECTION_IDS = 500

_EXPERIMENTAL = "Experimental: this route may change in a minor release."

# The router-level dependency is listed FIRST so it runs before the per-route
# session dependency: with the feature off an anonymous caller gets the same
# 404 as a signed-in one.
router = APIRouter(
    prefix="/me/workspace-slots",
    tags=["workspace-slots"],
    dependencies=[Depends(require_workspace_slots_enabled)],
)


# ---------------------------------------------------------------------------
# Response models (every one, nested ones included, serializes datetimes with Z)
# ---------------------------------------------------------------------------


class SlotAllowanceResponse(TZAwareBaseModel):
    """The owner's workspace cap (#1550), its parts and the next tier that adds slots."""

    cap: int
    base: int
    slot_bonus: int
    tier_grant: int
    tier: str
    next_tier: str | None = Field(description="null on the top tier")
    cap_on_next_tier: int | None = Field(description="null when next_tier is null")


class SlotSelectionResponse(TZAwareBaseModel):
    """The saved selection: owned workspace ids in saved order, and when it was saved."""

    workspace_ids: list[UUID]
    saved_at: datetime | None


class PaidPlansEndResponse(TZAwareBaseModel):
    """What would be active if every paid subscription of the owner ended.

    Every workspace under a billing contract becomes Free; admin-granted plans
    stay. Computed with the saved selection. Ids in priority order.
    """

    cap: int
    active: list[UUID]
    suspended: list[UUID]


class SlotWorkspaceResponse(TZAwareBaseModel):
    """One live workspace the owner owns."""

    id: UUID
    name: str
    plan_name: str
    status: Literal["active", "slot_suspended"]
    suspended_at: datetime | None
    under_billing_contract: bool
    is_current: bool
    created_at: datetime


class WorkspaceSlotsResponse(TZAwareBaseModel):
    """The owner's allowance, selection and owned workspaces.

    ``active_slots`` is how many workspaces may be active now
    (``max(cap, contracted_count, active_count)``); show "active
    ``active_count`` of ``active_slots``". ``workspaces`` is ordered by
    ``created_at``, then ``id``.
    """

    allowance: SlotAllowanceResponse
    owned_count: int
    active_count: int
    active_slots: int
    contracted_count: int
    selection_required: bool
    selection: SlotSelectionResponse
    version: int
    if_paid_plans_end: PaidPlansEndResponse | None = Field(
        description="null when no owned workspace is under a billing contract"
    )
    workspaces: list[SlotWorkspaceResponse]

    @classmethod
    def from_overview(cls, overview: SlotOverview) -> WorkspaceSlotsResponse:
        """Build the response from the service's ``SlotOverview``."""
        allowance = overview.allowance
        paid_end = overview.if_paid_plans_end
        return cls(
            allowance=SlotAllowanceResponse(
                cap=allowance.cap,
                base=allowance.base,
                slot_bonus=allowance.slot_bonus,
                tier_grant=allowance.tier_grant,
                tier=allowance.tier,
                next_tier=allowance.next_tier,
                cap_on_next_tier=allowance.cap_on_next_tier,
            ),
            owned_count=overview.owned_count,
            active_count=overview.active_count,
            active_slots=overview.active_slots,
            contracted_count=overview.contracted_count,
            selection_required=overview.selection_required,
            selection=SlotSelectionResponse(
                workspace_ids=list(overview.selection.workspace_ids),
                saved_at=overview.selection.saved_at,
            ),
            version=overview.version,
            if_paid_plans_end=(
                None
                if paid_end is None
                else PaidPlansEndResponse(
                    cap=paid_end.cap,
                    active=list(paid_end.active),
                    suspended=list(paid_end.suspended),
                )
            ),
            workspaces=[
                SlotWorkspaceResponse(
                    id=w.id,
                    name=w.name,
                    plan_name=w.plan_name,
                    status=w.status,
                    suspended_at=w.suspended_at,
                    under_billing_contract=w.under_billing_contract,
                    is_current=w.is_current,
                    created_at=w.created_at,
                )
                for w in overview.workspaces
            ],
        )


class SlotChangesResponse(TZAwareBaseModel):
    """Differences from the active set now (ids in priority order)."""

    suspend: list[UUID]
    resume: list[UUID]


class SlotPreviewResponse(TZAwareBaseModel):
    """The outcome of a selection; nothing is saved.

    ``active`` and ``suspended`` are the complete sets after applying;
    ``unmet`` the selected ids that stay suspended; ``changes`` only what
    differs from now.
    """

    scenario: Literal["current", "paid_plans_end"]
    cap: int
    active_slots: int
    active: list[UUID]
    suspended: list[UUID]
    unmet: list[UUID]
    changes: SlotChangesResponse

    @classmethod
    def from_preview(cls, preview: SelectionPreview) -> SlotPreviewResponse:
        """Build the response from the service's ``SelectionPreview``."""
        return cls(
            scenario=preview.scenario,
            cap=preview.cap,
            active_slots=preview.active_slots,
            active=list(preview.active),
            suspended=list(preview.suspended),
            unmet=list(preview.unmet),
            changes=SlotChangesResponse(
                suspend=list(preview.changes.suspend),
                resume=list(preview.changes.resume),
            ),
        )


class SlotContextResponse(TZAwareBaseModel):
    """One context the owner can export (show ``display_name`` or else ``name``)."""

    id: UUID
    name: str
    display_name: str | None
    is_private: bool


class SlotContextsResponse(TZAwareBaseModel):
    """The exportable contexts of one owned workspace."""

    workspace_id: UUID
    contexts: list[SlotContextResponse]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class SlotPreviewRequest(BaseModel):
    """A selection to preview: owned workspace ids in priority order."""

    workspace_ids: list[UUID] = Field(max_length=MAX_SELECTION_IDS)
    scenario: Literal["current", "paid_plans_end"]


class SlotSelectionRequest(BaseModel):
    """The selection to save, and the ``version`` the page read."""

    workspace_ids: list[UUID] = Field(max_length=MAX_SELECTION_IDS)
    expected_version: int = Field(ge=0)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=WorkspaceSlotsResponse,
    summary="Workspace allowance and owned workspaces",
    description=(
        "The signed-in owner's workspace allowance, saved selection and every live "
        "workspace they own with its status (active or slot_suspended). An owner of "
        "nothing gets 200 with an empty list. " + _EXPERIMENTAL
    ),
)
async def get_workspace_slots(
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceSlotsResponse:
    """Return the owner's slot overview.

    Args:
        user: Authenticated session user.
        db: Database session.

    Returns:
        The overview.
    """
    overview = await get_slot_overview(db, user["user_id"])
    return WorkspaceSlotsResponse.from_overview(overview)


@router.post(
    "/preview",
    response_model=SlotPreviewResponse,
    summary="Preview a workspace selection",
    description=(
        "Which owned workspaces would be active with this selection, now (scenario "
        "current) or once every paid subscription has ended (paid_plans_end). Saves "
        "nothing and checks no version. Duplicate ids or ids of workspaces the caller "
        "does not own answer 422 VAL-001 with details.invalid_ids. " + _EXPERIMENTAL
    ),
)
async def preview_workspace_selection(
    payload: SlotPreviewRequest,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> SlotPreviewResponse:
    """Preview a selection.

    Args:
        payload: The selection and the scenario.
        user: Authenticated session user.
        db: Database session.

    Returns:
        The preview.
    """
    preview = await preview_selection(db, user["user_id"], payload.workspace_ids, payload.scenario)
    return SlotPreviewResponse.from_preview(preview)


@router.put(
    "/selection",
    response_model=WorkspaceSlotsResponse,
    summary="Save which workspaces stay active",
    description=(
        "Replaces the saved ordered selection (an empty list means the default order; "
        "it clears selection_required now but not at a later drop) and applies it at "
        "once while anything is suspended (a swap or a resume within the allowance). "
        "Saving the list already stored keeps version and saved_at. Answers the full "
        "overview. 422 VAL-001 for duplicate or not-owned ids; 409 SLOT-003 when "
        "expected_version is stale; 409 SLOT-002 when workspaces with a paid "
        "subscription hold every active slot; 409 RES-002 (reason "
        "workspace_slots_busy) to retry; 404 RES-001 when the account was erased "
        "meanwhile. " + _EXPERIMENTAL
    ),
)
async def save_workspace_selection(
    payload: SlotSelectionRequest,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceSlotsResponse:
    """Save and apply the selection.

    ``save_selection`` takes the owner lock as the first statement of the
    transaction and commits itself.

    Args:
        payload: The selection and the version the page read.
        user: Authenticated session user.
        db: Database session.

    Returns:
        The overview after applying.
    """
    overview = await save_selection(
        db, user["user_id"], payload.workspace_ids, payload.expected_version
    )
    return WorkspaceSlotsResponse.from_overview(overview)


@router.get(
    "/workspaces/{workspace_id}/contexts",
    response_model=SlotContextsResponse,
    summary="Exportable contexts of an owned workspace",
    description=(
        "The contexts of a live workspace the caller owns, active or suspended, that "
        "the context export allows the caller to export: deleted contexts and other "
        "creators' private contexts are left out. A workspace that is not owned, "
        "deleted or unknown answers one 404 RES-001. " + _EXPERIMENTAL
    ),
)
async def list_exportable_contexts(
    workspace_id: UUID,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> SlotContextsResponse:
    """List the contexts of an owned workspace the owner can export.

    Args:
        workspace_id: The workspace.
        user: Authenticated session user.
        db: Database session.

    Returns:
        The contexts, ordered by name then id.

    Raises:
        NotFoundException: The workspace is not a live one the caller owns
            (one body for every case).
    """
    user_id = user["user_id"]
    owned = await db.execute(
        select(Workspace.id).where(
            Workspace.id == workspace_id,
            Workspace.owner_user_id == user_id,
            Workspace.deleted_at.is_(None),
        )
    )
    if owned.scalar_one_or_none() is None:
        raise NotFoundException("Workspace")

    # The export rule (``ContextService.export_context``): a private context
    # exports only for its creator or an account linked to the creator.
    rows = await db.execute(
        select(Context.id, Context.name, Context.display_name, Context.is_private)
        .where(
            Context.workspace_id == workspace_id,
            Context.deleted_at.is_(None),
            Context.is_private.is_(False) | owned_by(Context.created_by, user_id),
        )
        .order_by(Context.name, Context.id)
    )
    return SlotContextsResponse(
        workspace_id=workspace_id,
        contexts=[
            SlotContextResponse(
                id=row.id, name=row.name, display_name=row.display_name, is_private=row.is_private
            )
            for row in rows
        ],
    )
