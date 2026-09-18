"""Credential lane for the Sleep judge LLM (#1569).

Sleep has always resolved its judge key BYOK-then-env (a workspace's stored
OpenAI key first, the platform ``OPENAI_API_KEY`` second). Two deployment
settings change that:

- ``MANAGED_LLM_PROVIDER`` / ``MANAGED_LLM_MODEL`` become the judge's
  provider/model when no explicit ``SLEEP_LLM_*`` is set
  (``NeuralMemoryConfig.from_env``, flagged ``sleep_llm_from_managed_lane``).
- ``RESOLVE_STORED_BYOK_KEYS=false`` rules the stored keys out.

Only when BOTH hold do the judge calls ask ``LLMService`` for
``platform_only=True`` — the managed model is platform-paid and the
deployment has said stored keys are not to be used. Otherwise Sleep keeps its
BYOK-then-env resolution, ``paid_by`` default included (the known
mis-attribution is out of scope here).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from config.settings import get_settings

if TYPE_CHECKING:
    from neural.config import NeuralMemoryConfig


def judge_platform_only(config: NeuralMemoryConfig) -> bool:
    """Whether a Sleep judge call must resolve the platform credential only.

    Args:
        config: The run's ``NeuralMemoryConfig`` (its ``sleep_llm_*`` pair
            is what the phases pass to ``complete_json``).

    Returns:
        True when the judge runs on the managed lane AND the deployment set
        ``RESOLVE_STORED_BYOK_KEYS=false``; False keeps today's resolution.
    """
    if not config.sleep_llm_from_managed_lane:
        return False
    return not get_settings().resolve_stored_byok_keys
