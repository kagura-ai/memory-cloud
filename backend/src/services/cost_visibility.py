"""Deployment-level cost visibility (Issue #1571).

``ENABLE_COST_DISPLAY=false`` hides money from workspace users on a
flat-price hosted deployment: the workspace cost dashboard route answers
404 (``auth.dependencies.require_cost_display_enabled``) and the analysis
payloads drop their cost fields. This module owns the second half so the
REST lane (``api/routes/analyses.py``: ``AnalysisRow`` /
``AnalysisPreviewResponse``) and the MCP lane (``mcp_server/tools/analysis.py``:
``_serialize_run_row`` / ``analyze_context`` dry-run) cannot drift — the
same #1366 reasoning that put ``REDACTED_RUN_AGGREGATE_FIELDS`` in one place.

The two lanes differ deliberately in HOW they hide: REST keeps its
documented response shape and sends ``null`` (clients validate against a
schema); MCP dicts have no schema to keep stable, so the keys are omitted
and an agent never sees a money key at all.

``GET /admin/cost-aggregation`` is untouched by this flag — operators
still need to see what the platform spends.
"""

from __future__ import annotations

from typing import Any

# Every analysis-payload key that carries money, across both lanes: the run
# row columns (``cost_*``) and the pre-flight estimate (``estimated_cost_cents``).
ANALYSIS_COST_FIELDS: tuple[str, ...] = (
    "cost_estimated_cents",
    "cost_actual_cents",
    "estimated_cost_cents",
)


def cost_display_enabled() -> bool:
    """True when the deployment shows money to workspace users (``ENABLE_COST_DISPLAY``)."""
    from config.settings import get_settings

    return get_settings().enable_cost_display


def strip_cost_fields(payload: dict[str, Any], *, omit: bool) -> dict[str, Any]:
    """Hide the money keys in ``payload`` when the deployment disables cost display.

    Mutates and returns ``payload`` so it can wrap a dict literal in place.
    A no-op when ``ENABLE_COST_DISPLAY`` is true. Only keys already present
    are touched — a payload without cost keys never gains ``null`` ones.

    Args:
        payload: A serialized analysis dict (run row, preview, or the
            kwargs of one).
        omit: ``True`` removes the keys (MCP); ``False`` sets them to
            ``None`` so a schema-bound REST response keeps its shape.

    Returns:
        The same ``payload`` object, with the cost keys removed or nulled.
    """
    if cost_display_enabled():
        return payload
    for key in ANALYSIS_COST_FIELDS:
        if key not in payload:
            continue
        if omit:
            del payload[key]
        else:
            payload[key] = None
    return payload
