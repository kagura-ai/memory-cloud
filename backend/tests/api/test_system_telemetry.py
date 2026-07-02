"""Tests for /system/telemetry response shaping (#991).

Pins the security-relevant contract that the telemetry ``embedding_config``
payload exposes only public capability info and never the internal
``self_hosted_base_url`` to an authenticated, non-admin caller.
"""

from types import SimpleNamespace

from api.routes.system import _embedding_config_payload


def test_embedding_config_payload_excludes_internal_self_hosted_url():
    """embedding_config must expose only provider/model/dimensions — never the
    internal self_hosted_base_url, even when the provider is self_hosted (#991)."""
    settings = SimpleNamespace(
        embedding_provider="self_hosted",
        embedding_model="nomic-embed-text",
        embedding_dimensions=768,
        self_hosted_base_url="http://internal-backend:11434",
    )

    payload = _embedding_config_payload(settings)

    assert payload == {
        "provider": "self_hosted",
        "model": "nomic-embed-text",
        "dimensions": 768,
    }
    # Defence in depth: the internal URL must not leak via any key or value.
    assert "self_hosted_base_url" not in payload
    assert "internal-backend" not in str(payload)
