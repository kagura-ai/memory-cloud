"""Ratchet: the system role is compared with ``"admin"`` only in pinned places (#2767).

``SYSTEM_ADMIN_MODE`` holds only if every power of ``role = 'admin'`` goes
through ``require_admin`` or a helper of ``auth.system_admin_mode``
(``role_grants_system_admin`` / ``is_effective_session_admin``). A new
``role == "admin"`` (or ``== Role.ADMIN``) anywhere else under ``backend/src``
fails here: route it through a helper, or add it below with the reason it is
not a grant of system-admin power.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"

# file -> number of comparisons with "admin" / Role.ADMIN, and why each is fine.
PINNED: dict[str, int] = {
    # The helper itself does not compare with a literal operand (it uses a
    # module constant), so it is not listed. The gate's session branch:
    "auth/dependencies.py": 1,
    # is_initial_admin_role: the bootstrap marker follows the role new_account_role
    # chose (one comparison shared by the OAuth and email sign-up paths).
    "auth/roles.py": 1,
    # Validation of the requested role in PUT /admin/users/{id}/role.
    "api/routes/admin.py": 1,
    # Unreachable admin bypass (RateLimit runs outside SessionMiddleware).
    "api/middleware/rate_limit.py": 1,
    # Promote / demote / list system admins: they manage the role column.
    "services/system_admin_service.py": 6,
    # CLI-only batch minting keeps a users-row admin as inviter in every mode.
    "services/beta_invite_service.py": 1,
    # CLIs that create, delete or seed admin rows (shell access is the authority).
    "cli/create_admin.py": 1,
    "cli/delete_admin.py": 1,
    "cli/seed_e2e_admin.py": 1,
    # A workspace role (owner/admin/member/viewer), not the system role.
    "services/member_credentials_service.py": 1,
    # Dead helper (no callers); kept out of every request path.
    "utils/auth_helpers.py": 1,
}


def _is_admin_operand(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and node.value == "admin":
        return True
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "ADMIN"
        and isinstance(node.value, ast.Name)
        and node.value.id == "Role"
    )


def _comparisons() -> Counter[str]:
    found: Counter[str] = Counter()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and any(
                _is_admin_operand(operand) for operand in (node.left, *node.comparators)
            ):
                found[path.relative_to(SRC_ROOT).as_posix()] += 1
    return found


def test_admin_role_comparisons_stay_in_the_pinned_places():
    assert dict(_comparisons()) == PINNED


def test_the_scan_sees_a_new_inline_check(tmp_path):
    # The detector itself: a comparison in either spelling is counted.
    sample = 'if user.get("role") == "admin": pass\nif role != Role.ADMIN: pass\n'
    tree = ast.parse(sample)
    hits = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any(_is_admin_operand(o) for o in (node.left, *node.comparators))
    ]
    assert len(hits) == 2
