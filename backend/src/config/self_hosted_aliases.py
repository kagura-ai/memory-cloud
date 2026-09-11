"""Wire model ids for self-hosted embedding backends (#1525).

``EMBEDDING_MODEL_REGISTRY`` names a model the way this codebase does
(``qwen3-embedding:4b``) and derives the collection name, the dimensions and
the provider from that name. An OpenAI-compatible backend may serve the same
weights under its own id — a HF path on vLLM, a vendor prefix on a hosted
endpoint. ``SELF_HOSTED_MODEL_ALIASES`` maps one to the other at request time
only; nothing else in the system sees the upstream id, so the registry name
keeps being the single identity a context, a collection and a cache key hang
off.

Format: ``registry-name=upstream-id`` pairs, comma-separated. Entries without
an ``=``, or with an empty side, are ignored — an alias that cannot be parsed
falls back to sending the registry name, which the backend then rejects
loudly instead of silently embedding with a different model.
"""

from __future__ import annotations


def parse_self_hosted_model_aliases(raw: str | None) -> dict[str, str]:
    """Parse ``SELF_HOSTED_MODEL_ALIASES`` into ``{registry_name: upstream_id}``."""
    aliases: dict[str, str] = {}
    if not raw or not raw.strip():
        return aliases
    for entry in raw.split(","):
        if "=" not in entry:
            continue
        name, upstream = entry.split("=", 1)
        name, upstream = name.strip(), upstream.strip()
        if name and upstream:
            aliases[name] = upstream
    return aliases


def resolve_self_hosted_model_id(model: str, raw: str | None) -> str:
    """The ``model`` to put on the wire for a self-hosted request."""
    return parse_self_hosted_model_aliases(raw).get(model, model)
