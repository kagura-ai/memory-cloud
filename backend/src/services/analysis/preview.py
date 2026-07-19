"""Stage [A]: pre-flight cost estimate.

Called by the API layer (#496) before starting the pipeline so the
user can decide whether to spend the BYOK call. The full run only
fires after a separate explicit confirmation. The estimate must
return in <200ms so the modal stays responsive.

The estimate is intentionally conservative (rounds up token counts,
uses primary-model pricing not fallback): we'd rather show a higher
number and have the user feel pleasantly surprised than under-quote
and have a blown-budget complaint.

Cost model (gpt-5-nano default per Phase 3):

- One LLM call per cluster, plus 1 budget headroom call.
- Per-call: ~ ``5 reps * 60 tokens prompt + 80 tokens output`` =
  ``~380 input tokens + ~80 output tokens``.
- gpt-5-nano: $0.20/M input, $1.25/M output (per
  ``c03_471_seed_pricing.py:160``).
- Total: ``ceil(sqrt(n)) * (380 * 0.2 + 80 * 1.25) / 1_000_000``
  USD = ``ceil(sqrt(n)) * 0.000176`` USD.
- For n=8000: 90 clusters * 0.000176 = ~$0.0158 USD = ~2 cents.

The actual run also does the ~135k input-token "summary block"
because we send 5 reps' summaries per cluster. That dominates: each
call's input is closer to 1500 tokens, not 380. Updated:

- Input/call: 1500 tokens at $0.20/M = $0.0003
- Output/call: 80 tokens at $1.25/M = $0.0001
- Total/call: $0.0004
- Total/run: ``(ceil(sqrt(n)) + 1) * $0.0004``
- For n=8000: 91 * $0.0004 = ~$0.0364 USD = ~4 cents.

This matches the Phase 3 cost target ($0.033 / 8000 memory).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Default analysis provider + model (issue #496/B3 - shared by REST
# routes, MCP tools, orchestrator's pricing FK resolution, and the
# BYOK key assertion). v1.5 will replace these with per-workspace
# ``Workspace.analysis_default_model_id`` / ``analysis_default_provider``
# lookups; until then single named constants are the canonical source
# so route code, MCP code, orchestrator, and byok_resolver never drift
# out of sync on the provider/model pair.
DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL_ID = "gpt-5-nano"

# Per-call estimates. Tuned to match the Phase 3 cost target;
# revisit when Gemini integration lands in v1.5.
_INPUT_TOKENS_PER_CALL = 1500
_OUTPUT_TOKENS_PER_CALL = 80

# gpt-5-nano pricing per ``c03_471_seed_pricing`` (USD per million).
_GPT5_NANO_INPUT_PER_M = 0.20
_GPT5_NANO_OUTPUT_PER_M = 1.25


@dataclass(frozen=True)
class CostEstimate:
    """Output of Stage [A] surfaced via the preview API.

    Attributes:
        memory_count: Number of memories the analysis would cluster.
        cluster_count_estimate: ``ceil(sqrt(memory_count))``.
        estimated_cost_cents: Conservative upper-bound, integer cents.
        model_id: Pricing snapshot model the estimate is based on.
        breakdown: Itemized prompt/output token totals so the modal
            can show a "what counts as a token" tooltip if needed.
    """

    memory_count: int
    cluster_count_estimate: int
    estimated_cost_cents: int
    model_id: str
    breakdown: dict[str, int]


def assert_run_size_within_cap(memory_count: int, *, redact_count: bool = False) -> None:
    """Reject a run that would exceed ``ANALYSIS_MAX_MEMORY_COUNT`` (#1244).

    The pipeline materializes every matching vector into one in-RAM
    matrix and runs UMAP/KMeans inside the API process (the Option B
    analysis-worker externalization is not yet implemented), so run
    size directly bounds API-container memory/CPU — without a cap, one
    202-accepted request on a large context can OOM the multi-tenant
    API.

    Shared by REST ``/preview`` + ``POST /analyses`` and MCP
    ``analyze_context`` (both dry_run and real start), with a
    defense-in-depth re-check in ``vector_pull`` after the filtered
    set is known. v1 counts the FULL context — same semantics as the
    preview count, which ignores filters to stay under the 200 ms
    modal budget — so a filtered run on an over-cap context is
    rejected conservatively. Filter-aware counting is a v1.5
    follow-up.

    ``redact_count=True`` (#1366) keeps the cap decision on the TRUE
    count but omits the actual count from the 422 message and details —
    for enforce-mode agents the exact total is precisely the aggregate
    oracle the binding-scoped response counts exist to close, and the
    cap error must not reopen it. The cap value itself stays named (it
    is deployment config, not tenant data).

    Raises:
        ValidationError: 422 naming both the limit and the actual
            count so the client can render an actionable message
            (count omitted when ``redact_count``).
    """
    from config.settings import get_settings
    from utils.exceptions import ValidationError

    cap = int(get_settings().analysis_max_memory_count)
    if memory_count > cap:
        if redact_count:
            raise ValidationError(
                f"This analysis run would exceed the per-run analysis "
                f"cap of {cap} memories. Narrow the scope (filters, split "
                f"the context, or archive old memories).",
                field="memory_count",
                limit=cap,
            )
        # Copilot review: phrased as the RUN's memory count, not "the
        # context has" — the vector_pull defense-in-depth re-check calls
        # this with the FILTERED match count, where context-sized wording
        # would be misleading.
        raise ValidationError(
            f"This analysis run would include {memory_count} memories, "
            f"exceeding the per-run analysis cap of {cap}. Narrow the "
            f"scope (filters, split the context, or archive old memories) "
            f"or — on self-host — raise ANALYSIS_MAX_MEMORY_COUNT.",
            field="memory_count",
            memory_count=memory_count,
            limit=cap,
        )


def estimate_cost(memory_count: int, *, model_id: str = DEFAULT_MODEL_ID) -> CostEstimate:
    """Compute the pre-flight estimate for a memory_count-sized run.

    The model_id parameter is forward-compatibility scaffolding;
    today only gpt-5-nano is supported. v1.5 will branch on
    ``model_id`` to pull the actual ``llm_pricing`` row.
    """
    if memory_count < 2:
        # Pipeline cannot cluster a single memory. The API layer
        # should already reject this, but be defensive.
        return CostEstimate(
            memory_count=memory_count,
            cluster_count_estimate=0,
            estimated_cost_cents=0,
            model_id=model_id,
            breakdown={"input_tokens": 0, "output_tokens": 0},
        )

    n_clusters = max(2, math.ceil(math.sqrt(memory_count)))
    # +1 budget headroom call for the orchestrator's safety buffer.
    n_calls = n_clusters + 1

    input_tokens = n_calls * _INPUT_TOKENS_PER_CALL
    output_tokens = n_calls * _OUTPUT_TOKENS_PER_CALL

    cost_usd = (
        input_tokens * _GPT5_NANO_INPUT_PER_M + output_tokens * _GPT5_NANO_OUTPUT_PER_M
    ) / 1_000_000.0
    cost_cents = max(1, math.ceil(cost_usd * 100))

    return CostEstimate(
        memory_count=memory_count,
        cluster_count_estimate=n_clusters,
        estimated_cost_cents=cost_cents,
        model_id=model_id,
        breakdown={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "calls": n_calls,
        },
    )
