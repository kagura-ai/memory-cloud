"""Shared self-hosted backend probe for the CLI bootstrap scripts.

v0.42 review #20: ``setup_env`` and ``create_admin`` both auto-detect a running
self-hosted OpenAI-compatible backend the same way (bearer header + a GET to
``/v1/models``). This is the single implementation both call.
"""

from __future__ import annotations


def probe_self_hosted_available(base_url: str, api_key: str | None, *, timeout: int = 3) -> bool:
    """Return True iff ``{base_url}/v1/models`` answers 200 (Ollama / vLLM).

    Sends the bearer token so a vLLM backend started with ``--api-key`` answers
    instead of returning 401 (keyless Ollama ignores it). Never raises — any
    connection error / non-200 means "not available".
    """
    import urllib.request

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    req = urllib.request.Request(f"{base_url}/v1/models", method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed internal URL
            return resp.status == 200
    except Exception:
        return False
