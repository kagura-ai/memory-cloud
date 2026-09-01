"""Which embedding models this deployment offers (#1517).

``EMBEDDING_MODEL_REGISTRY`` says which models the *code* knows how to size and
route. That is not the same question as which models a given *deployment* will
actually serve. A deployment whose embedding provider is a self-hosted or
OpenAI-compatible endpoint cannot resolve the OpenAI entries at all unless the
workspace brings its own key. Advertising all of them, and accepting all of
them at context creation, lets a caller create a context that can never embed —
and because the embedding model is immutable after creation (#146), that
context cannot be repaired in place.

The allowlist is a deployment-level setting, deliberately empty by default so
existing deployments behave exactly as before.

Plan-tier policy ("3-large is Pro-only") is intentionally NOT decided here.
This module is the seam that makes such a policy a table edit later; today it
answers only "does this deployment offer that model at all".
"""

from __future__ import annotations

from config.constants import EMBEDDING_MODEL_REGISTRY


def allowed_embedding_models(allowlist_setting: str | None) -> tuple[str, ...]:
    """The models this deployment offers, in registry order.

    Args:
        allowlist_setting: Raw ``EMBEDDING_MODEL_ALLOWLIST`` value — a
            comma-separated list of model names. Empty or None means "no
            restriction", which is the default and preserves the pre-#1517
            behaviour of offering the whole registry.

    Returns:
        The allowed model names. Entries that are not in the registry are
        dropped: the registry is what the code can actually size and route, so
        a typo in the operator's list must not conjure a model into existence.
    """
    if not allowlist_setting or not allowlist_setting.strip():
        return tuple(EMBEDDING_MODEL_REGISTRY)

    requested = {entry.strip() for entry in allowlist_setting.split(",") if entry.strip()}
    # Iterate the registry (not the setting) so the result keeps a stable,
    # deployment-independent order and can only ever contain real models.
    return tuple(name for name in EMBEDDING_MODEL_REGISTRY if name in requested)


def is_embedding_model_allowed(model: str, allowlist_setting: str | None) -> bool:
    """Whether ``model`` may be selected on this deployment."""
    return model in allowed_embedding_models(allowlist_setting)
