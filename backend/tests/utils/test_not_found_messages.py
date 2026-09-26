"""404 messages say "not found" exactly once (#1693 item 2).

``NotFoundException(resource, resource_id=None)`` builds
``"<resource> not found[: <resource_id>]"`` itself. Call sites that passed a
whole sentence as ``resource`` ("Invitation not found", "Member not found:
<user> in workspace <ws>") produced "... not found not found", which both SDKs
print as sent. The guard below scans every call in ``src/`` so the pattern
cannot come back; the direct tests pin the message of a few fixed sites.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.context_service import ContextService
from services.invitation_service import InvitationService
from services.workspace_service import WorkspaceService
from utils.exceptions import NotFoundException

_SRC = Path(__file__).resolve().parents[2] / "src"
# "found" as a word: catches "X not found" and sentences such as
# "No context found. ..." — neither is a resource noun.
_FOUND = re.compile(r"\bfound\b", re.IGNORECASE)


def _not_found_calls() -> list[tuple[str, int, ast.Call]]:
    calls: list[tuple[str, int, ast.Call]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name == "NotFoundException":
                calls.append((str(path.relative_to(_SRC)), node.lineno, node))
    return calls


def _resource_arg(call: ast.Call) -> ast.expr | None:
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg == "resource":
            return kw.value
    return None


class TestNotFoundCallSiteGuard:
    def test_scan_sees_the_call_sites(self) -> None:
        # A broken scan (wrong root, renamed class) must not pass silently.
        assert len(_not_found_calls()) > 50

    def test_no_call_passes_a_message_as_the_resource(self) -> None:
        offenders = []
        for rel, lineno, call in _not_found_calls():
            arg = _resource_arg(call)
            if arg is not None and _FOUND.search(ast.unparse(arg)):
                offenders.append(f"{rel}:{lineno}: {ast.unparse(arg)}")
        assert not offenders, (
            "NotFoundException appends ' not found' itself — pass the resource "
            "noun (and the id as resource_id), not a message:\n" + "\n".join(offenders)
        )


class TestConstructor:
    def test_resource_only(self) -> None:
        assert NotFoundException("Invitation").message == "Invitation not found"

    def test_resource_and_id(self) -> None:
        exc = NotFoundException("Member", "u-1 in workspace w-1")
        assert exc.message == "Member not found: u-1 in workspace w-1"
        assert exc.status_code == 404
        assert exc.error_code == "RES-001"


def _db_returning_none() -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


class TestFixedCallSites:
    async def test_invitation_by_token(self) -> None:
        with pytest.raises(NotFoundException) as exc:
            await InvitationService(_db_returning_none()).get_invitation("missing-token")
        assert exc.value.message == "Invitation not found"

    async def test_delete_invitation(self) -> None:
        with pytest.raises(NotFoundException) as exc:
            await InvitationService(_db_returning_none()).delete_invitation(1, uuid4())
        assert exc.value.message == "Invitation not found"

    async def test_member(self) -> None:
        ws = uuid4()
        with pytest.raises(NotFoundException) as exc:
            await WorkspaceService(_db_returning_none()).get_member(ws, "ghost")
        assert exc.value.message == f"Member not found: ghost in workspace {ws}"

    async def test_workspace(self) -> None:
        ws = uuid4()
        with pytest.raises(NotFoundException) as exc:
            await WorkspaceService(_db_returning_none()).get_workspace(ws)
        assert exc.value.message == f"Workspace not found: {ws}"

    async def test_context(self) -> None:
        ctx = uuid4()
        with pytest.raises(NotFoundException) as exc:
            await ContextService(_db_returning_none()).get_context("u-1", ctx)
        assert exc.value.message == f"Context not found: {ctx}"
