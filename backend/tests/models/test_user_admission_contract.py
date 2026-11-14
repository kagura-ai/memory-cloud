"""The admission record's contract on ``users`` (#2803).

- the value set of ``auth.admission`` equals the ``valid_admitted_via`` CHECK,
  and what a gate can answer is a subset of what a row can hold;
- an ``AdmissionRecord`` carries invite details only for ``beta_invite`` and
  has no label;
- every ``User(...)`` built under ``backend/src`` passes ``admitted_via=``
  (an account is never created without saying how it was admitted);
- the inviter column is a ``SET NULL`` foreign key with an index, so deleting
  the inviter's account clears it and nothing else.
"""

from __future__ import annotations

import ast
import dataclasses
import re
import uuid
from pathlib import Path
from typing import get_args

import pytest
from sqlalchemy import CheckConstraint

from auth.admission import (
    ADMITTED_VIA_VALUES,
    AdmissionRecord,
    AdmittedVia,
    InviteIssuer,
    SignupAdmission,
)
from models.auth import User
from services.signup_gate_service import EmailSignupAdmission

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def _check(name: str) -> str:
    for constraint in User.__table__.constraints:
        if isinstance(constraint, CheckConstraint) and constraint.name == name:
            return str(constraint.sqltext)
    raise AssertionError(f"no CHECK {name} on users")


def test_value_set_equals_the_check_list() -> None:
    listed = re.findall(r"'([a-z_]+)'", _check("valid_admitted_via"))
    assert sorted(listed) == sorted(ADMITTED_VIA_VALUES)
    assert tuple(ADMITTED_VIA_VALUES) == get_args(AdmittedVia)
    assert len(ADMITTED_VIA_VALUES) == 7


def test_issuer_values_equal_the_check_list() -> None:
    listed = re.findall(r"'([a-z_]+)'", _check("valid_admitted_invite_issuer"))
    assert sorted(listed) == sorted(get_args(InviteIssuer)) == ["admin", "user"]


def test_gate_answers_are_a_subset_chain() -> None:
    email = set(get_args(EmailSignupAdmission))
    gate = set(get_args(SignupAdmission))
    stored = set(get_args(AdmittedVia))
    assert email <= gate <= stored
    # What only a backfill or a CLI can write.
    assert stored - gate == {"admin_cli", "unknown"}


def test_record_carries_invite_details_only_for_a_beta_invite() -> None:
    invite_id = uuid.uuid4()
    record = AdmissionRecord("beta_invite", beta_invite_id=invite_id, inviter_user_id="u-1")
    assert (record.via, record.beta_invite_id, record.inviter_user_id) == (
        "beta_invite",
        invite_id,
        "u-1",
    )
    assert AdmissionRecord("beta_invite").beta_invite_id is None
    with pytest.raises(ValueError):
        AdmissionRecord("allowlist", beta_invite_id=invite_id)
    with pytest.raises(ValueError):
        AdmissionRecord("workspace_invitation", inviter_user_id="u-1")


def test_record_is_frozen_and_has_no_label() -> None:
    record = AdmissionRecord("allowlist")
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.via = "beta_invite"  # type: ignore[misc]
    names = {f.name for f in dataclasses.fields(AdmissionRecord)}
    assert names == {"via", "beta_invite_id", "inviter_user_id"}
    assert not any("label" in name for name in names)


def _user_calls() -> list[tuple[str, int, set[str]]]:
    found: list[tuple[str, int, set[str]]] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "User"
            ):
                keywords = {kw.arg for kw in node.keywords if kw.arg is not None}
                found.append((path.relative_to(SRC_ROOT).as_posix(), node.lineno, keywords))
    return found


def test_every_users_row_writer_passes_admitted_via() -> None:
    calls = _user_calls()
    assert sorted({path for path, _, _ in calls}) == [
        "auth/roles.py",
        "cli/create_admin.py",
        "cli/seed_e2e_admin.py",
    ]
    assert len(calls) == 4
    missing = [(path, line) for path, line, keywords in calls if "admitted_via" not in keywords]
    assert missing == []


def test_inviter_column_is_a_set_null_foreign_key_with_an_index() -> None:
    column = User.__table__.c.admitted_by_user_id
    (fk,) = column.foreign_keys
    assert fk.target_fullname == "users.user_id"
    assert fk.ondelete == "SET NULL"
    assert column.index is True
    assert column.nullable is True


def test_columns_and_default() -> None:
    table = User.__table__
    via = table.c.admitted_via
    assert via.nullable is False
    assert via.server_default is not None
    assert "'unknown'" in str(via.server_default.arg)
    assert table.c.admitted_beta_invite_id.foreign_keys == set()
    assert table.c.admitted_beta_invite_id.nullable is True
    assert table.c.admitted_invite_issuer.nullable is True
    assert "label" not in " ".join(c.name for c in table.c if c.name.startswith("admitted"))


@pytest.mark.parametrize(
    ("name", "sql"),
    [
        (
            "admitted_invite_fields_beta_only",
            "admitted_via = 'beta_invite' OR (admitted_beta_invite_id IS NULL AND "
            "admitted_invite_issuer IS NULL AND admitted_by_user_id IS NULL)",
        ),
        (
            "admitted_by_user_issuer_user",
            "admitted_by_user_id IS NULL OR admitted_invite_issuer = 'user'",
        ),
    ],
)
def test_cross_column_checks(name: str, sql: str) -> None:
    assert _check(name) == sql
