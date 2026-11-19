"""The account routes of workspace slots, with the service patched (#2918).

``/api/v1/me/workspace-slots`` is the contract of design 12.1: the JSON of
every route is pinned here field for field (``Z`` timestamps on nested models
too), with every refusal body. The service functions are patched; the same
routes on real rows are in ``tests/integration/test_me_workspace_slots_db.py``.

- ``off`` / ``shadow``: every route of the surface answers 404 ``HTTP-404``
  before authentication (the body of an unknown path) and never opens a
  database session; other methods and the trailing slash get the framework's
  405 / 307.
- ``enforce``: browser session only (an API key gets 403, no credential 401).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import require_session_auth
from config.settings import get_settings
from db.base import get_db
from services.workspace_slots import (
    PaidPlansEndOutcome,
    SelectionPreview,
    SlotAllowanceView,
    SlotChanges,
    SlotOverview,
    SlotSelection,
    SlotWorkspace,
)
from utils.exceptions import (
    ValidationError,
    WorkspaceSlotsBusyError,
    WorkspaceSlotsFullError,
    WorkspaceSlotVersionConflictError,
)

BASE = "/api/v1/me/workspace-slots"
ROUTE = "api.routes.me_workspace_slots"
OWNER = "owner-2918"

PERSONAL = UUID("6f1c2a7e-3b0d-4d8e-9a51-0c2f6e1d9b10")
RESEARCH = UUID("a2d94c1b-55e7-4f0a-8c3e-7b6d2e9f4a21")
SIDE = UUID("d7e0b3f5-91c4-4a6b-b2d8-3e5f7a1c0c32")
STRANGER = UUID("0b6a9e44-2f1d-4c7a-9e3b-55d1c8a7f0e9")
TERM_END = datetime(2026, 12, 1, 0, 0, 12)

NOT_FOUND = {"error": "HTTP-404", "message": "Not Found", "details": {}}

ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", BASE, None),
    ("POST", f"{BASE}/preview", {"workspace_ids": [], "scenario": "current"}),
    ("PUT", f"{BASE}/selection", {"workspace_ids": [], "expected_version": 0}),
    ("GET", f"{BASE}/workspaces/{RESEARCH}/contexts", None),
]


def _after_term_overview(**overrides: Any) -> SlotOverview:
    """Design V3's first example: three owned Free workspaces, one slot."""
    values: dict[str, Any] = {
        "allowance": SlotAllowanceView(
            cap=1,
            base=1,
            slot_bonus=0,
            tier_grant=0,
            tier="free",
            next_tier="pro",
            cap_on_next_tier=3,
        ),
        "owned_count": 3,
        "active_count": 1,
        "active_slots": 1,
        "contracted_count": 0,
        "selection_required": True,
        "selection": SlotSelection(workspace_ids=(), saved_at=None),
        "version": 4,
        "if_paid_plans_end": None,
        "workspaces": (
            SlotWorkspace(
                id=PERSONAL,
                name="Personal",
                plan_name="free",
                status="active",
                suspended_at=None,
                under_billing_contract=False,
                is_current=True,
                created_at=datetime(2026, 3, 2, 9, 14),
            ),
            SlotWorkspace(
                id=RESEARCH,
                name="Research",
                plan_name="free",
                status="slot_suspended",
                suspended_at=TERM_END,
                under_billing_contract=False,
                is_current=False,
                created_at=datetime(2026, 5, 11, 2, 40, 31),
            ),
            SlotWorkspace(
                id=SIDE,
                name="Side project",
                plan_name="free",
                status="slot_suspended",
                suspended_at=TERM_END,
                under_billing_contract=False,
                is_current=False,
                created_at=datetime(2026, 8, 20, 11, 5, 47),
            ),
        ),
    }
    values.update(overrides)
    return SlotOverview(**values)


AFTER_TERM_JSON: dict[str, Any] = {
    "allowance": {
        "cap": 1,
        "base": 1,
        "slot_bonus": 0,
        "tier_grant": 0,
        "tier": "free",
        "next_tier": "pro",
        "cap_on_next_tier": 3,
    },
    "owned_count": 3,
    "active_count": 1,
    "active_slots": 1,
    "contracted_count": 0,
    "selection_required": True,
    "selection": {"workspace_ids": [], "saved_at": None},
    "version": 4,
    "if_paid_plans_end": None,
    "workspaces": [
        {
            "id": str(PERSONAL),
            "name": "Personal",
            "plan_name": "free",
            "status": "active",
            "suspended_at": None,
            "under_billing_contract": False,
            "is_current": True,
            "created_at": "2026-03-02T09:14:00Z",
        },
        {
            "id": str(RESEARCH),
            "name": "Research",
            "plan_name": "free",
            "status": "slot_suspended",
            "suspended_at": "2026-12-01T00:00:12Z",
            "under_billing_contract": False,
            "is_current": False,
            "created_at": "2026-05-11T02:40:31Z",
        },
        {
            "id": str(SIDE),
            "name": "Side project",
            "plan_name": "free",
            "status": "slot_suspended",
            "suspended_at": "2026-12-01T00:00:12Z",
            "under_billing_contract": False,
            "is_current": False,
            "created_at": "2026-08-20T11:05:47Z",
        },
    ],
}


@pytest.fixture
def mode(monkeypatch: pytest.MonkeyPatch):
    """Set ``WORKSPACE_SLOT_SUSPENSION`` on the cached settings for one test."""

    def _set(value: str) -> None:
        monkeypatch.setattr(get_settings(), "workspace_slot_suspension", value)
        if value == "enforce":
            monkeypatch.setattr(get_settings(), "enforce_workspace_cap", True)

    return _set


@pytest.fixture
def db_opened() -> Iterator[list[bool]]:
    """``get_db`` replaced by a mock session that records being opened."""
    opened: list[bool] = []

    async def _db():
        opened.append(True)
        yield MagicMock()

    app.dependency_overrides[get_db] = _db
    yield opened
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def anonymous(db_opened: list[bool]) -> Iterator[TestClient]:
    """No session override: the real ``require_session_auth`` runs."""
    yield TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def signed_in(db_opened: list[bool]) -> Iterator[TestClient]:
    """A signed-in session user."""

    async def _session() -> dict:
        return {"user_id": OWNER, "email": "owner@example.com", "role": "user"}

    app.dependency_overrides[require_session_auth] = _session
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.pop(require_session_auth, None)


def _call(client: TestClient, method: str, path: str, body: dict[str, Any] | None, **kw: Any):
    return client.request(method, path, json=body, **kw)


# ---------------------------------------------------------------------------
# Feature off / shadow
# ---------------------------------------------------------------------------


class TestFeatureOffAnswers404BeforeAuth:
    @pytest.mark.parametrize("value", ["off", "shadow"])
    @pytest.mark.parametrize(("method", "path", "body"), ROUTES)
    def test_anonymous_gets_the_404_of_a_missing_path(
        self, anonymous, db_opened, mode, value, method, path, body
    ) -> None:
        mode(value)
        response = _call(anonymous, method, path, body)
        assert response.status_code == 404
        assert response.json() == NOT_FOUND
        missing = anonymous.get("/api/v1/me/no-such-surface-2918")
        assert response.json() == missing.json()
        assert db_opened == []

    @pytest.mark.parametrize("value", ["off", "shadow"])
    @pytest.mark.parametrize(("method", "path", "body"), ROUTES)
    def test_signed_in_gets_the_same_404_and_no_session_is_opened(
        self, signed_in, db_opened, mode, value, method, path, body, monkeypatch
    ) -> None:
        mode(value)
        for name in ("get_slot_overview", "preview_selection", "save_selection"):
            monkeypatch.setattr(f"{ROUTE}.{name}", AsyncMock(side_effect=AssertionError(name)))
        response = _call(signed_in, method, path, body)
        assert response.status_code == 404
        assert response.json() == NOT_FOUND
        assert db_opened == []


# ---------------------------------------------------------------------------
# Enforce: authentication
# ---------------------------------------------------------------------------


class TestSessionOnly:
    @pytest.mark.parametrize(("method", "path", "body"), ROUTES)
    def test_no_credential_is_401(self, anonymous, mode, method, path, body) -> None:
        mode("enforce")
        response = _call(anonymous, method, path, body)
        assert response.status_code == 401

    @pytest.mark.parametrize(("method", "path", "body"), ROUTES)
    def test_an_api_key_is_refused(self, anonymous, mode, method, path, body) -> None:
        mode("enforce")
        response = _call(
            anonymous,
            method,
            path,
            body,
            headers={"Authorization": "Bearer kagura_" + "a" * 40},
        )
        assert response.status_code == 403
        assert response.json()["error"] == "HTTP-403"


# ---------------------------------------------------------------------------
# Enforce: GET
# ---------------------------------------------------------------------------


class TestGetOverview:
    def test_after_the_term_end_matches_the_contract(self, signed_in, mode, monkeypatch) -> None:
        mode("enforce")
        service = AsyncMock(return_value=_after_term_overview())
        monkeypatch.setattr(f"{ROUTE}.get_slot_overview", service)

        response = signed_in.get(BASE)

        assert response.status_code == 200, response.text
        assert response.json() == AFTER_TERM_JSON
        assert service.await_args.args[1] == OWNER

    def test_if_paid_plans_end_populated_and_saved_selection(
        self, signed_in, mode, monkeypatch
    ) -> None:
        mode("enforce")
        overview = _after_term_overview(
            allowance=SlotAllowanceView(
                cap=20,
                base=1,
                slot_bonus=0,
                tier_grant=19,
                tier="promax",
                next_tier=None,
                cap_on_next_tier=None,
            ),
            active_count=3,
            active_slots=20,
            contracted_count=1,
            selection_required=False,
            selection=SlotSelection(workspace_ids=(SIDE, PERSONAL), saved_at=TERM_END),
            if_paid_plans_end=PaidPlansEndOutcome(
                cap=1, active=(SIDE,), suspended=(PERSONAL, RESEARCH)
            ),
        )
        monkeypatch.setattr(f"{ROUTE}.get_slot_overview", AsyncMock(return_value=overview))

        body = signed_in.get(BASE).json()

        assert body["allowance"] == {
            "cap": 20,
            "base": 1,
            "slot_bonus": 0,
            "tier_grant": 19,
            "tier": "promax",
            "next_tier": None,
            "cap_on_next_tier": None,
        }
        assert body["selection"] == {
            "workspace_ids": [str(SIDE), str(PERSONAL)],
            "saved_at": "2026-12-01T00:00:12Z",
        }
        assert body["if_paid_plans_end"] == {
            "cap": 1,
            "active": [str(SIDE)],
            "suspended": [str(PERSONAL), str(RESEARCH)],
        }
        assert (body["active_count"], body["active_slots"], body["contracted_count"]) == (3, 20, 1)

    def test_an_owner_of_nothing_gets_200_and_an_empty_list(
        self, signed_in, mode, monkeypatch
    ) -> None:
        mode("enforce")
        overview = _after_term_overview(
            owned_count=0, active_count=0, selection_required=False, version=0, workspaces=()
        )
        monkeypatch.setattr(f"{ROUTE}.get_slot_overview", AsyncMock(return_value=overview))

        response = signed_in.get(BASE)

        assert response.status_code == 200
        body = response.json()
        assert body["workspaces"] == []
        assert body["if_paid_plans_end"] is None
        assert body["owned_count"] == 0


# ---------------------------------------------------------------------------
# Enforce: preview
# ---------------------------------------------------------------------------


def _preview() -> SelectionPreview:
    return SelectionPreview(
        scenario="paid_plans_end",
        cap=1,
        active_slots=1,
        active=(SIDE,),
        suspended=(PERSONAL, RESEARCH),
        unmet=(),
        changes=SlotChanges(suspend=(PERSONAL, RESEARCH), resume=(SIDE,)),
    )


def _invalid_ids(*ids: UUID) -> ValidationError:
    return ValidationError(
        "Only workspaces you own can be selected.",
        field="workspace_ids",
        invalid_ids=[str(i) for i in ids],
    )


class TestPreview:
    def test_matches_the_contract_and_passes_the_ids_in_order(
        self, signed_in, mode, monkeypatch
    ) -> None:
        mode("enforce")
        service = AsyncMock(return_value=_preview())
        monkeypatch.setattr(f"{ROUTE}.preview_selection", service)

        response = signed_in.post(
            f"{BASE}/preview",
            json={"workspace_ids": [str(SIDE), str(PERSONAL)], "scenario": "paid_plans_end"},
        )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "scenario": "paid_plans_end",
            "cap": 1,
            "active_slots": 1,
            "active": [str(SIDE)],
            "suspended": [str(PERSONAL), str(RESEARCH)],
            "unmet": [],
            "changes": {"suspend": [str(PERSONAL), str(RESEARCH)], "resume": [str(SIDE)]},
        }
        _db, owner, ids, scenario = service.await_args.args
        assert (owner, list(ids), scenario) == (OWNER, [SIDE, PERSONAL], "paid_plans_end")

    def test_duplicate_ids_reach_the_service_unchanged(self, signed_in, mode, monkeypatch) -> None:
        mode("enforce")
        service = AsyncMock(side_effect=_invalid_ids(SIDE))
        monkeypatch.setattr(f"{ROUTE}.preview_selection", service)

        response = signed_in.post(
            f"{BASE}/preview",
            json={"workspace_ids": [str(SIDE), str(SIDE)], "scenario": "current"},
        )

        assert list(service.await_args.args[2]) == [SIDE, SIDE]
        assert response.status_code == 422
        assert response.json() == {
            "error": "VAL-001",
            "message": "Only workspaces you own can be selected.",
            "details": {"field": "workspace_ids", "invalid_ids": [str(SIDE)]},
        }

    def test_an_unknown_scenario_is_a_body_422(self, signed_in, mode, monkeypatch) -> None:
        mode("enforce")
        monkeypatch.setattr(
            f"{ROUTE}.preview_selection", AsyncMock(side_effect=AssertionError("called"))
        )
        response = signed_in.post(
            f"{BASE}/preview", json={"workspace_ids": [], "scenario": "later"}
        )
        assert response.status_code == 422
        assert response.json()["error"] == "VAL-001"
        assert response.json()["message"] == "Request validation failed"


# ---------------------------------------------------------------------------
# Enforce: PUT selection
# ---------------------------------------------------------------------------


class TestSaveSelection:
    def test_answers_the_full_overview(self, signed_in, mode, monkeypatch) -> None:
        mode("enforce")
        service = AsyncMock(return_value=_after_term_overview())
        monkeypatch.setattr(f"{ROUTE}.save_selection", service)

        response = signed_in.put(
            f"{BASE}/selection",
            json={"workspace_ids": [str(RESEARCH), str(PERSONAL)], "expected_version": 4},
        )

        assert response.status_code == 200, response.text
        assert response.json() == AFTER_TERM_JSON
        _db, owner, ids, version = service.await_args.args
        assert (owner, list(ids), version) == (OWNER, [RESEARCH, PERSONAL], 4)

    def test_an_empty_list_is_allowed(self, signed_in, mode, monkeypatch) -> None:
        mode("enforce")
        service = AsyncMock(return_value=_after_term_overview())
        monkeypatch.setattr(f"{ROUTE}.save_selection", service)
        response = signed_in.put(
            f"{BASE}/selection", json={"workspace_ids": [], "expected_version": 0}
        )
        assert response.status_code == 200
        assert list(service.await_args.args[2]) == []

    def test_not_owned_is_422_with_the_invalid_ids(self, signed_in, mode, monkeypatch) -> None:
        mode("enforce")
        monkeypatch.setattr(
            f"{ROUTE}.save_selection", AsyncMock(side_effect=_invalid_ids(STRANGER))
        )
        response = signed_in.put(
            f"{BASE}/selection", json={"workspace_ids": [str(STRANGER)], "expected_version": 4}
        )
        assert response.status_code == 422
        assert response.json() == {
            "error": "VAL-001",
            "message": "Only workspaces you own can be selected.",
            "details": {"field": "workspace_ids", "invalid_ids": [str(STRANGER)]},
        }

    def test_a_stale_version_is_slot_003(self, signed_in, mode, monkeypatch) -> None:
        mode("enforce")
        monkeypatch.setattr(
            f"{ROUTE}.save_selection",
            AsyncMock(
                side_effect=WorkspaceSlotVersionConflictError(expected_version=4, current_version=5)
            ),
        )
        response = signed_in.put(
            f"{BASE}/selection", json={"workspace_ids": [str(RESEARCH)], "expected_version": 4}
        )
        assert response.status_code == 409
        assert response.json() == {
            "error": "SLOT-003",
            "message": (
                "Your workspaces changed since this page loaded. Review the list and save again."
            ),
            "details": {"expected_version": 4, "current_version": 5},
        }

    def test_contracted_workspaces_holding_every_slot_is_slot_002(
        self, signed_in, mode, monkeypatch
    ) -> None:
        mode("enforce")
        monkeypatch.setattr(
            f"{ROUTE}.save_selection",
            AsyncMock(
                side_effect=WorkspaceSlotsFullError(
                    cap=1,
                    active_slots=1,
                    active_count=1,
                    contracted_count=1,
                    next_tier="pro",
                    cap_on_next_tier=3,
                    unmet=[str(RESEARCH)],
                )
            ),
        )
        response = signed_in.put(
            f"{BASE}/selection", json={"workspace_ids": [str(RESEARCH)], "expected_version": 4}
        )
        assert response.status_code == 409
        assert response.json() == {
            "error": "SLOT-002",
            "message": (
                "All active slots are used by workspaces with a paid subscription. Upgrade a "
                "workspace, or delete one, to resume another."
            ),
            "details": {
                "cap": 1,
                "active_slots": 1,
                "active_count": 1,
                "contracted_count": 1,
                "next_tier": "pro",
                "cap_on_next_tier": 3,
                "unmet": [str(RESEARCH)],
            },
        }

    def test_a_busy_owner_lock_is_a_retryable_res_002(self, signed_in, mode, monkeypatch) -> None:
        mode("enforce")
        monkeypatch.setattr(
            f"{ROUTE}.save_selection", AsyncMock(side_effect=WorkspaceSlotsBusyError())
        )
        response = signed_in.put(
            f"{BASE}/selection", json={"workspace_ids": [], "expected_version": 4}
        )
        assert response.status_code == 409
        assert response.json()["error"] == "RES-002"
        assert response.json()["details"] == {"reason": "workspace_slots_busy"}

    @pytest.mark.parametrize(
        "body",
        [
            {"workspace_ids": []},
            {"workspace_ids": [], "expected_version": -1},
            {"workspace_ids": ["not-a-uuid"], "expected_version": 0},
            {"workspace_ids": [str(uuid4()) for _ in range(501)], "expected_version": 0},
        ],
        ids=["missing-version", "negative-version", "bad-id", "too-many-ids"],
    )
    def test_a_malformed_body_is_a_body_422(self, signed_in, mode, monkeypatch, body) -> None:
        mode("enforce")
        monkeypatch.setattr(
            f"{ROUTE}.save_selection", AsyncMock(side_effect=AssertionError("called"))
        )
        response = signed_in.put(f"{BASE}/selection", json=body)
        assert response.status_code == 422
        assert response.json()["error"] == "VAL-001"
        assert "errors" in response.json()["details"]


# ---------------------------------------------------------------------------
# Enforce: contexts (the not-owned case; the export rule is pinned on real rows)
# ---------------------------------------------------------------------------


class TestContexts:
    def test_a_workspace_that_is_not_owned_is_one_404(self, mode) -> None:
        mode("enforce")
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session = MagicMock()
        session.execute = AsyncMock(return_value=result)

        async def _db():
            yield session

        async def _session() -> dict:
            return {"user_id": OWNER, "email": "owner@example.com", "role": "user"}

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[require_session_auth] = _session
        try:
            client = TestClient(app, raise_server_exceptions=False)
            first = client.get(f"{BASE}/workspaces/{RESEARCH}/contexts")
            second = client.get(f"{BASE}/workspaces/{STRANGER}/contexts")
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(require_session_auth, None)

        assert first.status_code == 404
        assert first.json() == {
            "error": "RES-001",
            "message": "Workspace not found",
            "details": {},
        }
        assert second.json() == first.json()
        assert session.execute.await_count == 2


class TestFeatureOffOtherMethodsAndTheTrailingSlash:
    """What the framework's routing answers around the four routes with the
    feature off: the 405 of a method a route does not serve and the 307 of the
    trailing slash, as on any other path. No database session is opened."""

    @pytest.mark.parametrize("value", ["off", "shadow"])
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("DELETE", BASE),
            ("OPTIONS", BASE),
            ("GET", f"{BASE}/selection"),
            ("GET", f"{BASE}/preview"),
            ("POST", f"{BASE}/workspaces/{RESEARCH}/contexts"),
        ],
    )
    def test_an_unserved_method_is_405(self, anonymous, db_opened, mode, value, method, path):
        mode(value)
        response = anonymous.request(method, path)
        assert response.status_code == 405
        assert response.json()["error"] == "HTTP-405"
        assert db_opened == []

    @pytest.mark.parametrize("value", ["off", "shadow"])
    def test_the_trailing_slash_redirects_to_the_route(
        self, anonymous, db_opened, mode, value
    ) -> None:
        mode(value)
        response = anonymous.get(f"{BASE}/", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"].endswith(BASE)
        followed = anonymous.get(f"{BASE}/")
        assert followed.status_code == 404
        assert followed.json() == NOT_FOUND
        assert db_opened == []

    @pytest.mark.parametrize("value", ["off", "shadow"])
    def test_a_malformed_path_id_gets_the_same_404(self, anonymous, db_opened, mode, value) -> None:
        mode(value)
        response = anonymous.get(f"{BASE}/workspaces/not-a-uuid/contexts")
        assert response.status_code == 404
        assert response.json() == NOT_FOUND
        assert db_opened == []

    @pytest.mark.parametrize("value", ["off", "shadow"])
    @pytest.mark.parametrize(
        ("method", "path"), [("POST", f"{BASE}/preview"), ("PUT", f"{BASE}/selection")]
    )
    def test_a_body_that_is_not_json_is_422_before_the_404(
        self, anonymous, db_opened, mode, value, method, path
    ) -> None:
        """The framework reads the JSON body before any dependency runs, so a
        body it cannot parse answers its 422 as on every route."""
        mode(value)
        response = anonymous.request(
            method, path, content=b"{not json", headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 422
        assert response.json()["error"] == "VAL-001"
        assert db_opened == []
