"""Tests for /system/telemetry response shaping (#991).

Pins the security-relevant contract that the telemetry ``embedding_config``
payload exposes only public capability info and never the internal
``ollama_base_url`` to an authenticated, non-admin caller.
"""

from types import SimpleNamespace

from api.routes.system import _embedding_config_payload


def test_embedding_config_payload_excludes_internal_ollama_url():
    """embedding_config must expose only provider/model/dimensions — never the
    internal ollama_base_url, even when the provider is ollama (#991)."""
    settings = SimpleNamespace(
        embedding_provider="ollama",
        embedding_model="nomic-embed-text",
        embedding_dimensions=768,
        ollama_base_url="http://internal-ollama:11434",
    )

    payload = _embedding_config_payload(settings)

    assert payload == {
        "provider": "ollama",
        "model": "nomic-embed-text",
        "dimensions": 768,
    }
    # Defence in depth: the internal URL must not leak via any key or value.
    assert "ollama_base_url" not in payload
    assert "internal-ollama" not in str(payload)
