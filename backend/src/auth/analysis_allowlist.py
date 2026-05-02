"""Pure allowlist primitive for the Memory Broadlistening kill switch.

Split out of ``auth.analysis_gates`` (PR #538 / #497 review) so callers
that only need the membership check (e.g. ``api.routes.workspaces``
exposing ``analyses_enabled`` on the workspace response) do not pull
in the gate module's quota / tier / BYOK dependency tree.

The function reads ``settings.analysis_enabled_workspace_ids_list``
and is otherwise side-effect-free, so it remains safe to call from
unit tests via ``monkeypatch.setenv`` without a DB session.
"""

from __future__ import annotations

from uuid import UUID

from config.settings import get_settings


def check_workspace_in_allowlist(workspace_id: UUID | str) -> bool:
    """Return True iff the workspace is permitted to run analyses.

    Empty ``ANALYSIS_ENABLED_WORKSPACE_IDS`` (default) → returns False
    for every workspace, i.e. the feature is disabled platform-wide.
    Populated → only listed workspace UUIDs return True.
    """
    settings = get_settings()
    allowed = settings.analysis_enabled_workspace_ids_list
    if not allowed:
        return False
    # Both sides lower-cased so ops can paste env values in either case
    # without a silent kill-switch lockout (see settings property comment).
    return str(workspace_id).lower() in allowed
