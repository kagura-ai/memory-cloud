"""#2049: the self-hosted reachability probe is remembered per process.

``EmbeddingService`` is built fresh per memory (``process_pending_embedding``,
``context_routing``), so a per-instance "verified" flag let the
``GET /v1/models`` probe run on nearly every cache-miss embed, and an
occasional connect stall became a failed embed. The probe result now lives in
a module-level cache keyed by base URL, for a TTL no longer than the embedding
retry backoff.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services.llm_providers import self_hosted_provider as sh
from utils.exceptions import ConfigurationError, SelfHostedUnreachableError

pytestmark = pytest.mark.asyncio

BASE = "http://inference:8000"


def _http(*responses):
    """A patched ``httpx.AsyncClient`` whose ``get`` answers ``responses`` in order."""
    http = MagicMock()
    http.get = AsyncMock(side_effect=list(responses))
    http.__aenter__ = AsyncMock(return_value=http)
    http.__aexit__ = AsyncMock(return_value=False)
    return http


def _ok():
    resp = MagicMock()
    resp.status_code = 200
    return resp


def _status(code):
    resp = MagicMock()
    resp.status_code = code
    return resp


async def test_a_successful_probe_is_reused():
    http = _http(_ok())
    with patch("httpx.AsyncClient", return_value=http):
        await sh.ensure_self_hosted_reachable(BASE, None, label="inference server")
        await sh.ensure_self_hosted_reachable(BASE, None, label="inference server")
    assert http.get.await_count == 1


async def test_the_cache_expires_after_the_ttl():
    http = _http(_ok(), _ok())
    clock = [1000.0]
    with (
        patch("httpx.AsyncClient", return_value=http),
        patch.object(sh.time, "monotonic", side_effect=lambda: clock[0]),
    ):
        await sh.ensure_self_hosted_reachable(BASE, None)
        clock[0] += sh.REACHABILITY_TTL_SECONDS - 1
        await sh.ensure_self_hosted_reachable(BASE, None)
        assert http.get.await_count == 1
        clock[0] += 2
        await sh.ensure_self_hosted_reachable(BASE, None)
    assert http.get.await_count == 2


async def test_a_transport_error_is_unreachable_and_not_cached():
    """A connect stall raises the transient subclass and is probed again next time."""
    http = _http(httpx.ConnectTimeout("stall"), _ok())
    with patch("httpx.AsyncClient", return_value=http):
        with pytest.raises(SelfHostedUnreachableError) as err:
            await sh.ensure_self_hosted_reachable(BASE, None)
        # Still a ConfigurationError: CFG-001 on the API path, and the
        # embedding sweep retries it without spending the budget (#1496).
        assert isinstance(err.value, ConfigurationError)
        await sh.ensure_self_hosted_reachable(BASE, None)
    assert http.get.await_count == 2


@pytest.mark.parametrize("code", [401, 403, 404, 500])
async def test_a_misconfigured_server_fails_loud_every_time(code):
    """A server that answers with an error is misconfiguration, not a blip."""
    http = _http(_status(code), _status(code))
    with patch("httpx.AsyncClient", return_value=http):
        for _ in range(2):
            with pytest.raises(ConfigurationError) as err:
                await sh.ensure_self_hosted_reachable(BASE, None)
            assert not isinstance(err.value, SelfHostedUnreachableError)
    assert http.get.await_count == 2


async def test_the_cache_is_keyed_by_base_url_and_key():
    http = _http(_ok(), _ok(), _ok())
    with patch("httpx.AsyncClient", return_value=http):
        await sh.ensure_self_hosted_reachable(BASE, None)
        await sh.ensure_self_hosted_reachable("http://other:8000", None)
        await sh.ensure_self_hosted_reachable(BASE, "a-key")
        await sh.ensure_self_hosted_reachable(BASE, None)
    assert http.get.await_count == 3


def test_the_ttl_never_outlives_the_retry_backoff():
    """Within the TTL a dead server surfaces as a budget-spending provider error.

    Keeping the TTL at or below the backoff means every retry of a failed
    embedding probes again, so a server that went down stays a configuration
    failure (no budget spent) instead of exhausting MAX_EMBEDDING_RETRIES.
    """
    from config.constants import EMBEDDING_RETRY_BACKOFF_SECONDS

    assert 0 < sh.REACHABILITY_TTL_SECONDS <= EMBEDDING_RETRY_BACKOFF_SECONDS


async def test_fresh_embedding_services_share_one_probe():
    """The #2049 shape: one EmbeddingService per memory, one probe per process."""
    from services.embedding_service import EmbeddingService

    http = _http(_ok())
    with (
        patch("httpx.AsyncClient", return_value=http),
        patch("services.embedding_service.AsyncOpenAI"),
    ):
        for _ in range(3):
            svc = EmbeddingService(MagicMock())
            svc.provider = "self_hosted"
            svc.self_hosted_base_url = BASE
            svc.self_hosted_api_key = None
            await svc._get_client("u")
    assert http.get.await_count == 1


async def test_a_long_lived_service_probes_again_after_the_ttl():
    """No per-instance flag: a reused service (a Sleep reindex batch) re-checks."""
    from services.embedding_service import EmbeddingService

    http = _http(_ok(), _ok())
    clock = [1000.0]
    svc = EmbeddingService(MagicMock())
    svc.provider = "self_hosted"
    svc.self_hosted_base_url = BASE
    svc.self_hosted_api_key = None
    with (
        patch("httpx.AsyncClient", return_value=http),
        patch("services.embedding_service.AsyncOpenAI"),
        patch.object(sh.time, "monotonic", side_effect=lambda: clock[0]),
    ):
        await svc._get_client("u")
        clock[0] += sh.REACHABILITY_TTL_SECONDS + 1
        await svc._get_client("u")
    assert http.get.await_count == 2


# #2064: the LLM path (SelfHostedProvider._verify) shares the same cache.


def _provider(base_url=BASE, api_key=""):
    from services.llm_providers.self_hosted_provider import SelfHostedProvider

    return SelfHostedProvider(base_url=base_url, api_key=api_key)


async def test_fresh_llm_providers_share_one_probe():
    """A provider built per request or per task probes once within the TTL."""
    http = _http(_ok())
    with patch("httpx.AsyncClient", return_value=http):
        for _ in range(2):
            await _provider()._verify()
    assert http.get.await_count == 1


async def test_llm_and_embedding_paths_share_the_cache():
    """One server, one probe: a successful embedding probe also covers the LLM."""
    http = _http(_ok())
    with patch("httpx.AsyncClient", return_value=http):
        await sh.ensure_self_hosted_reachable(BASE, None, label="inference server")
        await _provider()._verify()
    assert http.get.await_count == 1


async def test_an_llm_probe_failure_is_not_cached():
    http = _http(httpx.ConnectTimeout("stall"), _ok())
    with patch("httpx.AsyncClient", return_value=http):
        with pytest.raises(SelfHostedUnreachableError) as err:
            await _provider()._verify()
        assert isinstance(err.value, ConfigurationError)
        assert "LLM backend" in str(err.value)
        await _provider()._verify()
    assert http.get.await_count == 2


async def test_an_llm_misconfigured_server_is_not_unreachable():
    http = _http(_status(401))
    with patch("httpx.AsyncClient", return_value=http):
        with pytest.raises(ConfigurationError) as err:
            await _provider(api_key="wrong")._verify()
    assert not isinstance(err.value, SelfHostedUnreachableError)


async def test_a_long_lived_llm_provider_does_not_probe_again():
    """The LLMService-held provider keeps its first success past the TTL."""
    http = _http(_ok())
    clock = [1000.0]
    provider = _provider()
    with (
        patch("httpx.AsyncClient", return_value=http),
        patch.object(sh.time, "monotonic", side_effect=lambda: clock[0]),
    ):
        await provider._verify()
        clock[0] += sh.REACHABILITY_TTL_SECONDS + 1
        await provider._verify()
    assert http.get.await_count == 1


async def test_a_trailing_slash_shares_the_cache_entry():
    """Embedding passes the setting as written; the LLM path strips the slash."""
    http = _http(_ok())
    with patch("httpx.AsyncClient", return_value=http):
        await sh.ensure_self_hosted_reachable(BASE + "/", None, label="inference server")
        await _provider(base_url=BASE + "/")._verify()
    assert http.get.await_count == 1


async def test_expired_entries_are_dropped_on_store():
    http = _http(_ok(), _ok())
    clock = [1000.0]
    with (
        patch("httpx.AsyncClient", return_value=http),
        patch.object(sh.time, "monotonic", side_effect=lambda: clock[0]),
    ):
        await sh.ensure_self_hosted_reachable("http://gone:8000", None)
        clock[0] += sh.REACHABILITY_TTL_SECONDS + 1
        await sh.ensure_self_hosted_reachable(BASE, None)
    assert list(sh._REACHABLE_UNTIL) == [sh._reachability_key(BASE, None)]
