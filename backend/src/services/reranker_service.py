"""Reranker service with multi-provider support.

Issue #105: Add multiple reranker options (Voyage AI, Cohere).
Issue #70: Add Ollama as local reranker provider.
Issue #1160: Renamed the local reranker provider key ollama → self_hosted and
normalized its transport to the OpenAI-compatible ``/v1/completions`` endpoint
(served by both Ollama and vLLM), so any self-hosted OpenAI-compatible backend
can drive prompt-based reranking.

Architecture:
- RerankerProvider: Abstract base class for reranker providers
- VoyageReranker: Voyage AI implementation (rerank-2.5-lite default)
- CohereReranker: Cohere implementation (rerank-multilingual-v3.0)
- SelfHostedReranker: Self-hosted OpenAI-compatible reranker (registers under
  the "self_hosted" provider key; no API key needed for keyless backends)
- VLLMReranker: Local true cross-encoder via an OpenAI/Jina-style /v1/rerank
  endpoint (vLLM/TEI/Infinity); selected when settings.rerank_base_url is set.
  Issue #2020: wraps query/documents in the Qwen3-Reranker prompt template
  when the served model is a Qwen3 reranker (RERANK_PROMPT_FORMAT)
  Issue #2033: SelfHostedReranker asks a Qwen3 reranker the same template on
  Ollama's /api/generate and scores the yes/no logprobs; a rerank whose scores
  carry no ranking raises RerankNotDiscriminatingError (fail open)
- RerankerService: Factory pattern to select active provider from DB
"""

from __future__ import annotations

import asyncio
import math
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.constants import RERANK_PROMPT_FORMATS
from models.auth import ExternalAPIKey
from repositories.config_repository import ContextSearchConfigRepository
from utils.encryption import get_encryptor
from utils.exceptions import CohereError, VoyageError
from utils.logger import get_logger
from utils.url_redact import redact_generic_url

if TYPE_CHECKING:
    from config.settings import Settings

logger = get_logger(__name__)

# Provider constants (must match external_keys.py for API-key providers)
RERANKER_PROVIDERS = {"cohere", "voyage"}

# Hard-floor default self-hosted (Ollama-engine) reranker model. This is an
# Ollama-registry model id, so a pure-vLLM self-hosted stack MUST override it
# via settings.self_hosted_rerank_model (env SELF_HOSTED_RERANK_MODEL) — the
# ops-level default that normally supplies this value; this constant is only
# the fallback when that setting is explicitly blanked. Keep the two in sync.
DEFAULT_SELF_HOSTED_RERANK_MODEL = "dengcao/Qwen3-Reranker-8B:Q5_K_M"

# Built-in fallback model name served behind settings.rerank_base_url
# (e.g. vLLM `--served-model-name`). The ops-level default is
# settings.rerank_model (env RERANK_MODEL, same default value); this constant
# is the hard floor used when RERANK_MODEL is explicitly blanked. Keep the two
# literals in sync.
DEFAULT_VLLM_RERANK_MODEL = "qwen3-reranker-0.6b"

# Qwen3-Reranker prompt template (#2020). The model is trained on this exact
# chat layout; fed raw text, its yes/no logit barely separates relevant from
# irrelevant documents and rerank does worse than no rerank. Split the way the
# model card and vLLM's qwen3_reranker example build a score / rerank request:
# the query carries the system prefix + instruction + query, every document
# carries the document + the assistant suffix, and the server concatenates them.
QWEN3_RERANK_PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based on "
    'the Query and the Instruct provided. Note that the answer can only be "yes" or '
    '"no".<|im_end|>\n<|im_start|>user\n'
)
QWEN3_RERANK_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
# The model card's default task instruction, used when RERANK_INSTRUCTION
# (settings.rerank_instruction) is empty — the only copy of this literal.
DEFAULT_QWEN3_RERANK_INSTRUCTION = (
    "Given a web search query, retrieve relevant passages that answer the query"
)

# Text the Qwen3 tokenizer turns into a control token (#2020 review). Every
# added token of the Qwen3 tokenizer (tokenizer_config.json
# added_tokens_decoder) is matched as ONE token whether or not it is flagged
# special, so stored memory text containing "<|im_end|>\n<|im_start|>assistant"
# could close the Document slot and write the model's "yes" itself. All the
# "<|...|>" tokens start with "<|"; the rest are the tag-shaped ones below.
# Broken by a space after "<": the text stays readable, the token never forms,
# and a single non-overlapping pass cannot reassemble one ("<<|" -> "<< |").
_QWEN3_CONTROL_TOKEN_RE = re.compile(r"<(?=\|)|<(?=/?(?:think|tool_call|tool_response)>)")


def _neutralise_qwen3_control_tokens(text: str) -> str:
    """Break every Qwen3 added-token spelling in caller-supplied text."""
    return _QWEN3_CONTROL_TOKEN_RE.sub("< ", text)


class RerankNotDiscriminatingError(RuntimeError):
    """A rerank whose scores carry no ranking (#2033).

    Every output was unparseable or failed, or every candidate scored 0 (the
    measured all-"0" of a Qwen3 reranker behind the generic score prompt).
    Writing those scores over hybrid_score would erase the hybrid order's
    signal, so the call raises and SearchService fails open
    (``rerank_failed_open``) to the un-reranked results.
    """


class RerankGenerateUnsupportedError(RuntimeError):
    """SELF_HOSTED_BASE_URL serves no Ollama ``/api/generate`` (HTTP 404, #2033).

    The qwen3 format of SelfHostedReranker needs Ollama's native API; a vLLM
    backend 404s it. Raised for the whole call (one clear failure, logged once
    per process as ``self_hosted_rerank_generate_unsupported``) instead of
    every document failing on its own.
    """


# self_hosted_rerank_generate_unsupported is logged once per process.
_GENERATE_UNSUPPORTED_WARNED = False


# Character budget for a document body in the qwen3 format (#2020 gate2). The
# template's suffix (<|im_end|> + the assistant turn + the empty think block)
# must reach the model: a server that truncates an over-long input from the
# right cuts it off and the yes/no score is meaningless. So the body is cut
# here, before the suffix is appended. 2000 characters keeps a whole memory
# summary (capped at 500) and most of its context summary — the generic score
# prompt (SelfHostedReranker raw) scores only doc[:500] — and is at most ~2000
# tokens even for CJK text, so prefix + query + document + suffix fit a
# 4096-token --max-model-len (or Ollama num_ctx).
QWEN3_RERANK_MAX_DOC_CHARS = 2000


def _qwen3_query_part(query: str, instruction: str) -> str:
    """System prefix + instruction + neutralised query (the template's head)."""
    # Stored memories and the caller's query are untrusted: neutralise the
    # tokenizer's control tokens before they enter the template.
    safe_query = _neutralise_qwen3_control_tokens(query)
    return f"{QWEN3_RERANK_PREFIX}<Instruct>: {instruction}\n<Query>: {safe_query}\n"


def _qwen3_document_part(doc: str) -> str:
    """Truncated, neutralised document + assistant suffix (the template's tail)."""
    # Truncate the body (not the formatted string) so the suffix survives;
    # cutting first also means a split token ("<|im_") is no token at all.
    body = _neutralise_qwen3_control_tokens(doc[:QWEN3_RERANK_MAX_DOC_CHARS])
    return f"<Document>: {body}{QWEN3_RERANK_SUFFIX}"


def _resolve_rerank_format(prompt_format: str, model: str) -> str:
    """``auto`` -> ``qwen3`` for a Qwen3 reranker model name, else ``raw``.

    Raises:
        ValueError: If prompt_format is not one of auto, qwen3, raw.
    """
    if prompt_format not in RERANK_PROMPT_FORMATS:
        raise ValueError(
            f"prompt_format must be one of {sorted(RERANK_PROMPT_FORMATS)}, got {prompt_format!r}"
        )
    if prompt_format == "auto":
        return "qwen3" if _is_qwen3_reranker(model) else "raw"
    return prompt_format


# How many alternatives Ollama returns for the first generated token (#2033).
# "yes"/"no" and their case/space variants sit at the top for a working Qwen3
# reranker; 20 is the OpenAI-compatible maximum.
SELF_HOSTED_QWEN3_TOP_LOGPROBS = 20


def _is_ollama_model_error(response: httpx.Response) -> bool:
    """Whether a 404 is Ollama's ``{"error": "model '…' not found"}`` (vs. no route)."""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — a non-JSON 404 is no Ollama model error
        return False
    error = body.get("error") if isinstance(body, dict) else None
    return isinstance(error, str) and "model" in error.lower()


def _qwen3_yes_probability(payload: Any) -> float | None:
    """P(yes) / (P(yes) + P(no)) from an Ollama /api/generate reply (#2033).

    Reads the first generated token's ``top_logprobs`` (plus the token itself)
    and sums the probability of every spelling of "yes" and of "no"
    (case-insensitive, surrounding whitespace ignored: the 8B GGUF puts most
    of its mass on " no" / " No"). The ratio is the model card's score. A
    missing word counts as probability 0.

    Returns:
        The score in [0, 1], or ``None`` when the reply carries no logprobs
        (Ollama < 0.12.11 ignores the field) or neither word is among the
        alternatives (a model that does not answer yes/no).
    """
    if not isinstance(payload, dict):
        return None
    logprobs = payload.get("logprobs")
    if not isinstance(logprobs, list) or not logprobs or not isinstance(logprobs[0], dict):
        return None
    first = logprobs[0]
    candidates = [first, *(first.get("top_logprobs") or [])]
    by_token: dict[str, float] = {}
    for c in candidates:
        if not isinstance(c, dict):
            continue
        token, lp = c.get("token"), c.get("logprob")
        if isinstance(token, str) and isinstance(lp, int | float) and not isinstance(lp, bool):
            by_token.setdefault(token, float(lp))
    p_yes = sum(math.exp(lp) for t, lp in by_token.items() if t.strip().lower() == "yes")
    p_no = sum(math.exp(lp) for t, lp in by_token.items() if t.strip().lower() == "no")
    if p_yes + p_no <= 0.0:
        return None
    return p_yes / (p_yes + p_no)


# Calibration smoke check (#2020): with the qwen3 format, one relevant and one
# irrelevant document must separate by at least this much, else
# rerank_model_uncalibrated. Templated Qwen3-Reranker-0.6B scores the pair
# ~0.997 / ~0.000; the same model on raw text ~0.980 / ~0.866. For raw the score
# scale is unknown (TEI/Infinity may return logits), so scores are only logged.
RERANK_CALIBRATION_MIN_MARGIN = 0.5
_CALIBRATION_QUERY = "What is the capital of France?"
_CALIBRATION_RELEVANT = "Paris is the capital of France."
_CALIBRATION_IRRELEVANT = "Bananas are yellow fruit."
# Waits before the 2nd/3rd attempt: the reranker container may still be loading
# the model when the API boots.
_CALIBRATION_RETRY_DELAYS: tuple[float, ...] = (10.0, 30.0)


def _is_qwen3_reranker(model: str) -> bool:
    """Whether a served model name names a Qwen3 reranker (case-insensitive)."""
    return "qwen3-reranker" in model.lower().replace("_", "-")


# Default model names of the REMOTE reranker providers (Voyage, Cohere). A
# per-context reranker_model still carrying one of these is a stale default left
# over from a previous remote provider — on the self-hosted / vLLM path it must
# be re-resolved to the local model, not sent verbatim. Single source shared by
# both resolution branches below.
REMOTE_RERANKER_DEFAULT_MODELS = frozenset(
    {
        "rerank-2",
        "rerank-2-lite",
        "rerank-2.5",
        "rerank-2.5-lite",
        "rerank-multilingual-v3.0",
        "rerank-english-v3.0",
    }
)

# The model each REMOTE provider gets when a context leaves reranker_model
# empty. Keep the voyage entry equal to the ORM column default
# (models/config.py) — it is also what `search_config_defaults` writes when the
# deployment sets no DEFAULT_RERANKER_MODEL.
_REMOTE_PROVIDER_DEFAULT_MODELS = {
    "voyage": "rerank-2",
    "cohere": "rerank-multilingual-v3.0",
}


def default_reranker_model_for(provider: str, settings: Settings | None = None) -> str:
    """Default model for ``provider`` when no explicit reranker_model is set (#1572).

    Single source for every default-writing site (context creation,
    ``create_or_get``, ``reset_to_default``, the MCP ``update_search_config``
    fallbacks, ``/system/info``) and for ``_get_reranker_model``'s runtime
    fallback, so the provider/model pairing cannot drift between them.

    Args:
        provider: ``voyage`` | ``cohere`` | ``self_hosted``.
        settings: Settings to read the self-hosted model from. Loaded lazily
            when omitted so remote-provider lookups never touch settings.

    Returns:
        The model name to send to that provider. For ``self_hosted`` it is
        ``RERANK_MODEL`` when ``RERANK_BASE_URL`` is set (batched /v1/rerank),
        else ``SELF_HOSTED_RERANK_MODEL`` (prompt scoring); the built-in
        constants are the floor when either is explicitly blanked.
    """
    if provider == "self_hosted":
        if settings is None:
            from config.settings import get_settings

            settings = get_settings()
        if settings.rerank_base_url:
            return settings.rerank_model or DEFAULT_VLLM_RERANK_MODEL
        return settings.self_hosted_rerank_model or DEFAULT_SELF_HOSTED_RERANK_MODEL
    return _REMOTE_PROVIDER_DEFAULT_MODELS.get(provider, "rerank-2")


def _bearer_headers(api_key: str | None) -> dict[str, str] | None:
    """Authorization header for a self-hosted OpenAI-compatible backend behind
    auth (vLLM --api-key etc.), or ``None`` for a keyless backend (Ollama)."""
    return {"Authorization": f"Bearer {api_key}"} if api_key else None


class RerankerProvider(ABC):
    """Abstract base class for reranker providers.

    All reranker implementations must inherit from this class
    and implement the rerank() method.
    """

    provider_name: str

    @abstractmethod
    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[dict[str, Any]]:
        """Rerank documents by relevance to query.

        Args:
            query: Search query
            documents: List of document texts to rerank
            top_n: Number of top results to return

        Returns:
            List of dicts with 'index' and 'relevance_score' keys,
            sorted by relevance (descending)
        """
        pass


class VoyageReranker(RerankerProvider):
    """Voyage AI reranker implementation.

    Uses rerank-2.5-lite model by default (cost-effective).
    API Reference: https://docs.voyageai.com/docs/reranker

    Issue #105: Voyage AI integration for cost reduction.
    """

    provider_name = "voyage"

    def __init__(self, api_key: str, model: str = "rerank-2.5-lite"):
        """Initialize Voyage reranker.

        Args:
            api_key: Voyage AI API key
            model: Model name (default: rerank-2.5-lite, also: rerank-2.5)
        """
        self.api_key = api_key
        self.model = model

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[dict[str, Any]]:
        """Rerank using Voyage AI API.

        Note: Voyage client is synchronous, so we run in executor.

        Args:
            query: Search query
            documents: Documents to rerank
            top_n: Number of results

        Returns:
            Reranked results with index and relevance_score

        Raises:
            VoyageError: If API call fails
            ValueError: If top_n <= 0
        """
        # Input validation
        if not documents:
            return []
        if top_n <= 0:
            raise ValueError("top_n must be positive")

        try:
            import voyageai  # type: ignore[reportMissingImports]

            def _sync_rerank():
                client = voyageai.Client(api_key=self.api_key)  # pyright: ignore[reportPrivateImportUsage]
                return client.rerank(
                    query=query,
                    documents=documents,
                    model=self.model,
                    top_k=top_n,
                )

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, _sync_rerank)

            return [
                {"index": r.index, "relevance_score": r.relevance_score} for r in response.results
            ]
        except ValueError:
            raise
        except Exception as e:
            logger.error(
                "voyage_rerank_failed",
                error=str(e),
                model=self.model,
                query_length=len(query),
                doc_count=len(documents),
            )
            raise VoyageError(f"Reranking failed for model {self.model}: {e}") from e


class CohereReranker(RerankerProvider):
    """Cohere reranker implementation.

    Uses rerank-multilingual-v3.0 model by default.
    API Reference: https://docs.cohere.com/reference/rerank
    """

    provider_name = "cohere"

    def __init__(self, api_key: str, model: str = "rerank-multilingual-v3.0"):
        """Initialize Cohere reranker.

        Args:
            api_key: Cohere API key
            model: Model name (default: rerank-multilingual-v3.0)
        """
        self.api_key = api_key
        self.model = model

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[dict[str, Any]]:
        """Rerank using Cohere API.

        Args:
            query: Search query
            documents: Documents to rerank
            top_n: Number of results

        Returns:
            Reranked results with index and relevance_score

        Raises:
            CohereError: If API call fails
            ValueError: If top_n <= 0
        """
        # Input validation
        if not documents:
            return []
        if top_n <= 0:
            raise ValueError("top_n must be positive")

        try:
            import cohere

            client = cohere.AsyncClient(self.api_key)

            response = await client.rerank(
                model=self.model,
                query=query,
                documents=documents,
                top_n=top_n,
            )

            return [
                {"index": r.index, "relevance_score": r.relevance_score} for r in response.results
            ]
        except ValueError:
            raise
        except Exception as e:
            logger.error(
                "cohere_rerank_failed",
                error=str(e),
                model=self.model,
                query_length=len(query),
                doc_count=len(documents),
            )
            raise CohereError(f"Reranking failed for model {self.model}: {e}") from e


class SelfHostedReranker(RerankerProvider):
    """Self-hosted reranker scoring each document with a local LLM (Issue #70).

    Two request formats (``prompt_format``, RERANK_PROMPT_FORMAT, #2033):

    - ``qwen3`` (``auto`` for a Qwen3 reranker model name — the default
      model is one): the #2020 Qwen3-Reranker template is sent to Ollama's
      native ``/api/generate`` with ``raw`` (no Modelfile chat template on
      top) and ``logprobs``; the score is P(yes) / (P(yes) + P(no)) of the
      first generated token. Needs Ollama 0.12.11+; vLLM does not serve
      ``/api/generate`` (use RERANK_BASE_URL there).
    - ``raw``: the generic "Score the relevance ... Score: " prompt on the
      OpenAI-compatible ``/v1/completions`` (Ollama and vLLM), the number in
      the reply parsed as the score — for general instruction models. A
      Qwen3 reranker answers it with "0" or "!!!!!" (measured, #2033).

    Memory text and the query are run through the Qwen3 control-token
    neutraliser in both formats. A call whose scores carry no ranking —
    every output unparseable, or every document scored the same — raises
    (``RerankNotDiscriminatingError``) so recall fails open instead of
    overwriting every hybrid_score with one value.

    No API key required for keyless backends; an optional bearer token
    supports backends started with ``--api-key`` (e.g. vLLM). Registers under
    the "self_hosted" provider key (Issue #1160).
    """

    provider_name = "self_hosted"

    def __init__(
        self,
        base_url: str,
        model: str = DEFAULT_SELF_HOSTED_RERANK_MODEL,
        api_key: str | None = None,
        prompt_format: str = "auto",
        instruction: str | None = None,
    ):
        """Initialize the self-hosted reranker.

        Args:
            base_url: Backend base URL (e.g. http://localhost:11434 for
                Ollama, http://localhost:8000 for vLLM)
            model: Model name for reranking
            api_key: Optional bearer token for backends that require one
                (e.g. vLLM launched with ``--api-key``)
            prompt_format: ``auto`` | ``qwen3`` | ``raw`` (RERANK_PROMPT_FORMAT).
                ``auto`` resolves to ``qwen3`` when ``model`` names a Qwen3
                reranker, else ``raw`` (#2033).
            instruction: Qwen3 ``<Instruct>`` text (RERANK_INSTRUCTION); blank
                or ``None`` = the model card default.

        Raises:
            ValueError: If prompt_format is not one of auto, qwen3, raw.
        """
        self.base_url = base_url.rstrip("/")
        # Log/exception-safe form: SELF_HOSTED_BASE_URL may carry user:pass@.
        self.safe_base_url = redact_generic_url(self.base_url)
        self.model = model
        self.api_key = api_key or None
        self.prompt_format = _resolve_rerank_format(prompt_format, model)
        self.instruction = (instruction or "").strip() or DEFAULT_QWEN3_RERANK_INSTRUCTION

    async def _score_one(self, client: httpx.AsyncClient, query: str, doc: str) -> float | None:
        """Score one document; ``None`` when the reply holds no usable score.

        Raises:
            httpx.HTTPError: If the backend call fails.
            Exception: If the reply body has an unexpected shape.
        """
        headers = _bearer_headers(self.api_key)
        if self.prompt_format == "qwen3":
            resp = await client.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": _qwen3_query_part(query, self.instruction)
                    + _qwen3_document_part(doc),
                    "raw": True,
                    "stream": False,
                    "logprobs": True,
                    "top_logprobs": SELF_HOSTED_QWEN3_TOP_LOGPROBS,
                    "options": {"num_predict": 1, "temperature": 0},
                },
                headers=headers,
            )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404 and not _is_ollama_model_error(exc.response):
                    raise RerankGenerateUnsupportedError(
                        f"self_hosted rerank: {self.safe_base_url}/api/generate returned 404 — "
                        "the backend has no Ollama native API (vLLM?). The qwen3 format "
                        "needs Ollama >= 0.12.11; for vLLM set RERANK_BASE_URL (/v1/rerank)"
                    ) from exc
                raise
            return _qwen3_yes_probability(resp.json())

        safe_query = _neutralise_qwen3_control_tokens(query)
        safe_doc = _neutralise_qwen3_control_tokens(doc[:500])
        resp = await client.post(
            f"{self.base_url}/v1/completions",
            json={
                "model": self.model,
                "prompt": (
                    "Score the relevance of the document to the query "
                    "on a scale of 0 to 1.\n\n"
                    f"Query: {safe_query}\n"
                    f"Document: {safe_doc}\n\n"
                    "Score: "
                ),
                "max_tokens": 5,
                "temperature": 0,
                "stop": ["\n"],
            },
            headers=headers,
        )
        resp.raise_for_status()
        return _try_parse_relevance_score(resp.json()["choices"][0]["text"])

    async def score_documents(
        self, query: str, documents: list[str]
    ) -> list[float | None | Exception]:
        """Score every document; no fail-safe applied (the calibration check reads this).

        Returns:
            One entry per document, in order: the score, ``None`` for an
            unparseable reply, or the exception the request raised (logged
            as ``self_hosted_rerank_score_failed``, except
            ``RerankGenerateUnsupportedError``, which ``rerank`` reports once).
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            semaphore = asyncio.Semaphore(5)  # Limit concurrent backend requests

            async def score_doc(idx: int, doc: str) -> float | None | Exception:
                async with semaphore:
                    try:
                        return await self._score_one(client, query, doc)
                    except RerankGenerateUnsupportedError as e:
                        return e
                    except Exception as e:  # noqa: BLE001 — one doc never fails the call
                        # No str(e): httpx messages embed the request URL, which
                        # may carry SELF_HOSTED_BASE_URL credentials.
                        logger.warning(
                            "self_hosted_rerank_score_failed",
                            index=idx,
                            error_class=type(e).__name__,
                            status_code=(
                                e.response.status_code
                                if isinstance(e, httpx.HTTPStatusError)
                                else None
                            ),
                            endpoint=self.safe_base_url,
                        )
                        return e

            return list(await asyncio.gather(*(score_doc(i, d) for i, d in enumerate(documents))))

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[dict[str, Any]]:
        """Rerank by scoring each document individually.

        Args:
            query: Search query
            documents: Documents to rerank
            top_n: Number of results

        Returns:
            Reranked results with index and relevance_score

        Raises:
            ValueError: If top_n is non-positive.
            RerankGenerateUnsupportedError: If the backend 404s /api/generate
                (qwen3 format on a non-Ollama backend).
            RuntimeError: If every document scoring failed (backend down).
            RerankNotDiscriminatingError: If no output was parseable.
        """
        if not documents:
            return []
        if top_n <= 0:
            raise ValueError("top_n must be positive")

        outcomes = await self.score_documents(query, documents)
        n = len(documents)
        unsupported = next(
            (o for o in outcomes if isinstance(o, RerankGenerateUnsupportedError)), None
        )
        if unsupported is not None:
            global _GENERATE_UNSUPPORTED_WARNED
            if not _GENERATE_UNSUPPORTED_WARNED:
                _GENERATE_UNSUPPORTED_WARNED = True
                logger.warning(
                    "self_hosted_rerank_generate_unsupported",
                    model=self.model,
                    endpoint=self.safe_base_url,
                    hint="set RERANK_BASE_URL for vLLM /v1/rerank",
                )
            raise unsupported
        failed = sum(1 for o in outcomes if isinstance(o, Exception))
        unparseable = sum(1 for o in outcomes if o is None)
        if unparseable:
            # Once per call with a count — not one line per document.
            rerank_format = self.prompt_format
            logger.warning(
                "self_hosted_rerank_unparseable",
                model=self.model,
                rerank_format=rerank_format,
                unparseable_count=unparseable,
                failed_count=failed,
                doc_count=n,
            )

        # When EVERY scoring failed the backend is down (or 404s the model)
        # and an all-zero list would be returned in arbitrary order as if it
        # were a ranking — raise instead so SearchService fails open to the
        # un-reranked hybrid results and emits rerank_failed_open (#1572).
        if failed == n:
            raise RuntimeError(
                f"self_hosted rerank backend unavailable: all {n} "
                f"document scorings failed (model={self.model})"
            )
        if failed + unparseable == n:
            raise RerankNotDiscriminatingError(
                f"self_hosted rerank produced no usable score: {unparseable} of {n} "
                f"outputs unparseable, {failed} failed (model={self.model}, "
                f"format={self.prompt_format})"
            )
        # A partial failure / unparseable reply keeps a zero for that document.
        # Whether the scores rank at all is RerankerService's shared guard.
        scores = [o if isinstance(o, float) else 0.0 for o in outcomes]
        scored = [{"index": i, "relevance_score": sc} for i, sc in enumerate(scores)]
        scored.sort(key=lambda x: x["relevance_score"], reverse=True)

        logger.debug(
            "self_hosted_rerank_completed",
            model=self.model,
            doc_count=n,
            top_n=top_n,
        )

        return scored[:top_n]


class VLLMReranker(RerankerProvider):
    """True cross-encoder reranker via an OpenAI/Jina-style /v1/rerank endpoint.

    One batched HTTP call scores all documents — unlike SelfHostedReranker's
    per-document /v1/completions prompt scoring. Served by vLLM (``--runner
    pooling`` sequence-classification), HF TEI, or Infinity. Selected by
    setting ``settings.rerank_base_url``; the context config stays
    ``reranker_provider="self_hosted"`` — the context declares "local rerank
    on", the deployment decides how it is served (kagura-memory-eval GB10 rig).
    """

    provider_name = "vllm"

    def __init__(
        self,
        base_url: str,
        model: str = DEFAULT_VLLM_RERANK_MODEL,
        api_key: str | None = None,
        prompt_format: str = "auto",
        instruction: str | None = None,
    ):
        """Initialize the /v1/rerank client.

        Args:
            base_url: Endpoint base URL (e.g. http://gpu:8002); /v1/rerank is appended.
            model: Served model name on the rerank endpoint.
            api_key: Optional bearer token (RERANK_API_KEY) for a /v1/rerank
                endpoint served behind auth (e.g. vLLM --api-key). Without it,
                every request to a secured endpoint 401s and rerank silently
                falls back to unranked results.
            prompt_format: ``auto`` | ``qwen3`` | ``raw`` (RERANK_PROMPT_FORMAT).
                ``auto`` resolves to ``qwen3`` when ``model`` names a Qwen3
                reranker, else ``raw`` (#2020).
            instruction: Qwen3 ``<Instruct>`` text (RERANK_INSTRUCTION); blank
                or ``None`` = the model card default.

        Raises:
            ValueError: If prompt_format is not one of auto, qwen3, raw.
        """
        self.prompt_format = _resolve_rerank_format(prompt_format, model)
        self.base_url = base_url.rstrip("/")
        # Log/exception-safe form: RERANK_BASE_URL may carry user:pass@.
        self.safe_base_url = redact_generic_url(self.base_url)
        self.model = model
        self.api_key = api_key
        self.instruction = (instruction or "").strip() or DEFAULT_QWEN3_RERANK_INSTRUCTION

    def _format(self, query: str, documents: list[str]) -> tuple[str, list[str]]:
        """Apply the resolved prompt format to the query and documents."""
        if self.prompt_format != "qwen3":
            return query, documents
        formatted_query = _qwen3_query_part(query, self.instruction)
        formatted_docs = [_qwen3_document_part(doc) for doc in documents]
        return formatted_query, formatted_docs

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[dict[str, Any]]:
        """Rerank all documents with one batched /v1/rerank request.

        Args:
            query: Search query
            documents: Documents to rerank
            top_n: Number of results

        Returns:
            Reranked results with index and relevance_score (descending)

        Raises:
            ValueError: If top_n is non-positive or the /v1/rerank response has an unexpected shape
            httpx.HTTPError: If the rerank endpoint call fails
        """
        if not documents:
            return []
        if top_n <= 0:
            raise ValueError("top_n must be positive")

        # Clamp to the candidate count: some OpenAI/Jina-style rerank servers
        # reject top_n > len(documents) with a 4xx. Mirrors the Ollama path,
        # which never asks for more results than it has documents.
        effective_top_n = min(top_n, len(documents))

        headers = _bearer_headers(self.api_key)
        sent_query, sent_documents = self._format(query, documents)

        # v0.42 review #31: a per-call AsyncClient is deliberate here, not a
        # cached one. Rerank runs at most one HTTP request per call, so the
        # client-setup cost is negligible against the network round-trip, and a
        # module-level shared client would bind to a single event loop — fine in
        # the app but a cross-loop failure/flake source in tests and worker
        # forks. The connection-reuse win does not justify that fragility.
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.base_url}/v1/rerank",
                json={
                    "model": self.model,
                    "query": sent_query,
                    "documents": sent_documents,
                    # Every score, not the top_n cut: RerankerService's shared
                    # no-signal guard needs the full list (#2033).
                    "top_n": len(documents),
                },
                headers=headers,
            )
            resp.raise_for_status()
            payload = resp.json()
            # Validate the OpenAI/Jina-style shape before indexing so an
            # unexpected body (proxy error page, different schema) fails with a
            # clear message instead of an opaque KeyError/TypeError.
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list):
                keys = list(payload)[:5] if isinstance(payload, dict) else None
                raise ValueError(
                    f"Unexpected /v1/rerank response from {self.safe_base_url}: "
                    f"expected a 'results' list, got {type(payload).__name__}"
                    + (f" with keys {keys}" if keys is not None else "")
                )

        # Validate each item's shape too — a list whose elements lack
        # 'index'/'relevance_score' (or have a non-numeric score, or an index
        # that isn't an in-range int) would otherwise raise an opaque
        # KeyError/TypeError/IndexError downstream (RerankerService does
        # ``candidates[idx]``), losing the clear diagnostics added for the
        # top-level shape check. Coerce + bounds-check the index here.
        scored: list[dict[str, Any]] = []
        n = len(documents)
        for i, r in enumerate(results):
            if not isinstance(r, dict) or "index" not in r or "relevance_score" not in r:
                raise ValueError(
                    f"Unexpected /v1/rerank result item {i} from {self.safe_base_url}: "
                    "expected an object with 'index' and 'relevance_score'"
                )
            # index must be an int (reject bool, which is an int subclass) in
            # [0, len(documents)) so the downstream candidates[idx] is safe.
            idx = r["index"]
            if isinstance(idx, bool) or not isinstance(idx, int) or not (0 <= idx < n):
                raise ValueError(
                    f"Unexpected /v1/rerank result item {i} from {self.safe_base_url}: "
                    f"'index' must be an int in [0, {n}), got {idx!r}"
                )
            try:
                score = float(r["relevance_score"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Unexpected /v1/rerank result item {i} from {self.safe_base_url}: "
                    f"'relevance_score' is not a number ({r.get('relevance_score')!r})"
                ) from exc
            scored.append({"index": idx, "relevance_score": score})
        scored.sort(key=lambda x: x["relevance_score"], reverse=True)

        logger.debug(
            "vllm_rerank_completed",
            model=self.model,
            prompt_format=self.prompt_format,
            doc_count=len(documents),
            top_n=effective_top_n,
        )

        return scored[:effective_top_n]


def should_run_rerank_calibration(settings: Settings) -> bool:
    """Whether the startup calibration check has anything to check (#2020, #2033).

    Only when the deployment has not switched reranking off
    (ENABLE_RERANKING) — the same deployment-level gate SearchService applies
    before any rerank — and a local rerank path is in play: a /v1/rerank
    endpoint (RERANK_BASE_URL), or, without one, a deployment whose new
    contexts default to the self-hosted reranker with reranking on
    (DEFAULT_RERANKER_PROVIDER=self_hosted and DEFAULT_USE_RERANK=true,
    scored by SelfHostedReranker on SELF_HOSTED_BASE_URL) — without
    DEFAULT_USE_RERANK the check would load an 8B model at boot for nothing. SELF_HOSTED_BASE_URL alone does not count: it also
    serves self-hosted embeddings, and pinging a reranker model nobody uses
    would make Ollama load it. The RERANK_BASE_URL path keeps #2020's gate:
    whether a context reranks is per-context config (``use_rerank``), so
    DEFAULT_USE_RERANK, which only seeds new contexts, does not gate it there.
    """
    if not settings.enable_reranking:
        return False
    if settings.rerank_base_url:
        return True
    return (
        getattr(settings, "default_reranker_provider", None) == "self_hosted"
        and getattr(settings, "default_use_rerank", False) is True
    )


def _is_transient_rerank_error(exc: BaseException) -> bool:
    """Connect / timeout / HTTP 5xx: worth retrying while the reranker boots."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


def _log_calibration_failed(reranker: VLLMReranker | SelfHostedReranker, **fields: Any) -> None:
    """WARNING for a calibration check that produced no verdict (redacted URL)."""
    # A format name, not prompt text (the #1720 log-redaction test keys on names).
    rerank_format = reranker.prompt_format
    logger.warning(
        "rerank_calibration_check_failed",
        provider=reranker.provider_name,
        model=reranker.model,
        rerank_format=rerank_format,
        endpoint=reranker.safe_base_url,
        **fields,
    )


def _calibration_reranker(settings: Settings) -> VLLMReranker | SelfHostedReranker:
    """The reranker recalls use under these settings, built from the ops-level model."""
    if settings.rerank_base_url:
        return VLLMReranker(
            base_url=settings.rerank_base_url,
            model=settings.rerank_model or DEFAULT_VLLM_RERANK_MODEL,
            api_key=settings.rerank_api_key or None,
            prompt_format=settings.rerank_prompt_format,
            instruction=settings.rerank_instruction,
        )
    return SelfHostedReranker(
        base_url=settings.self_hosted_base_url,
        model=settings.self_hosted_rerank_model or DEFAULT_SELF_HOSTED_RERANK_MODEL,
        api_key=settings.self_hosted_api_key or None,
        prompt_format=settings.rerank_prompt_format,
        instruction=settings.rerank_instruction,
    )


async def _calibration_scores(
    reranker: VLLMReranker | SelfHostedReranker,
) -> tuple[dict[int, float], int]:
    """Score the calibration pair: ``({index: score}, unparseable_count)``.

    SelfHostedReranker is read through ``score_documents`` (no fail-safe): an
    unparseable or identical pair is the uncalibrated signal itself, not a
    failed check. An unparseable output counts as 0.0.

    Raises:
        Exception: A transient request error on ANY document (retried by the
            caller), or the first error when every document failed.
    """
    docs = [_CALIBRATION_RELEVANT, _CALIBRATION_IRRELEVANT]
    if isinstance(reranker, VLLMReranker):
        results = await reranker.rerank(_CALIBRATION_QUERY, docs, top_n=2)
        return {r["index"]: r["relevance_score"] for r in results}, 0
    outcomes = await reranker.score_documents(_CALIBRATION_QUERY, docs)
    by_index: dict[int, float] = {}
    unparseable = 0
    errors: list[Exception] = []
    for i, o in enumerate(outcomes):
        if isinstance(o, Exception):
            if _is_transient_rerank_error(o):
                raise o
            errors.append(o)
        elif o is None:
            unparseable += 1
            by_index[i] = 0.0
        else:
            by_index[i] = o
    if errors and not by_index:
        raise errors[0]
    return by_index, unparseable


async def run_rerank_calibration_check(
    settings: Settings | None = None,
    retry_delays: tuple[float, ...] = _CALIBRATION_RETRY_DELAYS,
) -> dict[str, Any] | None:
    """Smoke-check that the local reranker separates relevant from irrelevant (#2020, #2033).

    Scores one relevant and one irrelevant document for a fixed query through
    the reranker recalls use — ``VLLMReranker`` when RERANK_BASE_URL is set,
    else ``SelfHostedReranker`` on SELF_HOSTED_BASE_URL — with the same prompt
    format and the ops-level model (``RERANK_MODEL`` / ``SELF_HOSTED_RERANK_MODEL``;
    a per-context ``reranker_model`` override is not checked). A gap below
    ``RERANK_CALIBRATION_MIN_MARGIN`` logs ``rerank_model_uncalibrated`` at
    WARNING. That applies to the /v1/rerank qwen3 format (a template applied
    twice, a model that is not a Qwen3 reranker) and to both self-hosted
    formats, whose scores are always in [0, 1] (an all-"0" or unparseable
    score prompt, an Ollama without logprobs). With /v1/rerank raw the score
    scale is unknown, so the scores are only logged
    (``rerank_calibration_observed``, info).

    Run once per process as a background task at startup, never on the recall
    path. Fail-soft: transient errors (connect, timeout, HTTP 5xx) are retried
    after each ``retry_delays`` entry; a permanent one (HTTP 4xx, a malformed
    body) or the last failure logs ``rerank_calibration_check_failed`` at
    WARNING and gives up. Nothing raises.

    Args:
        settings: Settings to read RERANK_* / SELF_HOSTED_* from; loaded when omitted.
        retry_delays: Seconds to wait before each retry after a transient failure.

    Returns:
        ``{"calibrated", "relevant_score", "irrelevant_score", "margin"}`` —
        ``calibrated`` is ``None`` for the /v1/rerank raw format — or ``None``
        when the check does not apply (see ``should_run_rerank_calibration``)
        or failed.
    """
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    if not should_run_rerank_calibration(settings):
        return None
    reranker = _calibration_reranker(settings)

    delays = list(retry_delays)
    while True:
        try:
            by_index, unparseable = await _calibration_scores(reranker)
            break
        except Exception as exc:  # noqa: BLE001 — best-effort startup check
            if delays and _is_transient_rerank_error(exc):
                await asyncio.sleep(delays.pop(0))
                continue
            # No str(exc): httpx messages and the shape errors embed the
            # request URL, which may carry RERANK_BASE_URL credentials.
            # RerankGenerateUnsupportedError wraps the 404 it was raised from.
            http_exc = exc if isinstance(exc, httpx.HTTPStatusError) else exc.__cause__
            _log_calibration_failed(
                reranker,
                error_class=type(exc).__name__,
                status_code=(
                    http_exc.response.status_code
                    if isinstance(http_exc, httpx.HTTPStatusError)
                    else None
                ),
            )
            return None

    if set(by_index) != {0, 1}:
        # A missing score is not a 0.0 score: one result could otherwise
        # pass as "calibrated".
        _log_calibration_failed(
            reranker, error_class="IncompleteResults", result_count=len(by_index)
        )
        return None
    relevant = by_index[0]
    irrelevant = by_index[1]
    margin = relevant - irrelevant
    fields: dict[str, Any] = {
        "provider": reranker.provider_name,
        "model": reranker.model,
        "prompt_format": reranker.prompt_format,
        "relevant_score": round(relevant, 4),
        "irrelevant_score": round(irrelevant, 4),
        "margin": round(margin, 4),
    }
    if unparseable:
        fields["unparseable_count"] = unparseable
    calibrated: bool | None
    if isinstance(reranker, VLLMReranker) and reranker.prompt_format != "qwen3":
        calibrated = None
        logger.info("rerank_calibration_observed", **fields)
    else:
        calibrated = margin >= RERANK_CALIBRATION_MIN_MARGIN
        fields["min_margin"] = RERANK_CALIBRATION_MIN_MARGIN
        if calibrated:
            logger.info("rerank_model_calibrated", **fields)
        else:
            logger.warning("rerank_model_uncalibrated", **fields)
    return {
        "calibrated": calibrated,
        "relevant_score": relevant,
        "irrelevant_score": irrelevant,
        "margin": margin,
    }


def _try_parse_relevance_score(text: str) -> float | None:
    """Parse a relevance score from model output.

    Handles formats: "0.8", "0.8/1", "80%", "0", "1", etc.

    Returns:
        Float between 0.0 and 1.0, or ``None`` when the text holds no number
        (#2033: an unparseable reply is not a zero score)
    """
    text = text.strip()
    if not text:
        return None

    # Try percentage anywhere: "80%" -> 0.8
    pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if pct_match:
        return min(float(pct_match.group(1)) / 100.0, 1.0)

    # Try fraction anywhere: "0.8/1" -> 0.8
    frac_match = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", text)
    if frac_match:
        num, den = float(frac_match.group(1)), float(frac_match.group(2))
        return min(num / den, 1.0) if den > 0 else 0.0

    # Try plain number anywhere (clamp to 0.0-1.0)
    num_match = re.search(r"(\d+(?:\.\d+)?)", text)
    if num_match:
        return min(float(num_match.group(1)), 1.0)

    return None


class RerankerService:
    """Service for dynamic reranker provider selection.

    Issue #105: Factory pattern to select active provider based on
    enabled ExternalAPIKey configuration.

    Usage:
        reranker_service = RerankerService(db)
        results = await reranker_service.rerank(
            query="search query",
            candidates=search_results,
            user_id="user123",
            k=10,
            context_id="context-uuid"
        )
    """

    def __init__(self, db: AsyncSession):
        """Initialize reranker service.

        Args:
            db: Database session
        """
        self.db = db

    async def get_active_provider(
        self,
        user_id: str,
        context_id: str | None = None,
        workspace_id: str | None = None,  # NEW: Workspace ID (Issue #146)
    ) -> RerankerProvider | None:
        """Get the active reranker provider for the current workspace context.

        Queries ExternalAPIKey for the enabled reranker key. Issue #105's
        cross-provider exclusivity (one of Cohere / Voyage per workspace) is
        enforced at runtime by validate_reranker_exclusivity call sites in
        external_keys.py (create / toggle). The a99 migration has a matching
        pre-flight check, but that check only runs once during upgrade — it
        is a legacy-data safeguard, not a runtime enforcement mechanism.

        Issue #385: key resolution is workspace-keyed. When workspace_id is
        omitted, the reranker is treated as not configured (returns None) —
        there is no user-scoped or global fallback tier anymore.

        Priority within a workspace: context-scoped > workspace-scoped.

        Args:
            user_id: Caller's user ID — logged for audit, NOT used as a filter (#385).
            context_id: Optional context UUID (context-scoped keys take priority).
            workspace_id: Workspace UUID; when omitted returns None.

        Returns:
            RerankerProvider instance, or None if no reranker key configured.
        """
        from uuid import UUID

        from sqlalchemy import or_

        # Issue #70: Check context config first — self_hosted takes priority (no API key needed)
        if context_id:
            from config.settings import get_settings
            from models.config import ContextSearchConfig

            ctx_result = await self.db.execute(
                select(ContextSearchConfig).where(
                    ContextSearchConfig.context_id == UUID(context_id)
                )
            )
            ctx_config = ctx_result.scalar_one_or_none()
            if (
                ctx_config
                and ctx_config.reranker_provider == "self_hosted"
                and ctx_config.use_rerank
            ):
                settings = get_settings()
                # Avoid non-local default models left over from a previous
                # remote provider (Voyage / Cohere) — otherwise a stale remote
                # default model name would be sent to the local reranker.
                non_self_hosted_defaults = REMOTE_RERANKER_DEFAULT_MODELS
                model = ctx_config.reranker_model
                if settings.rerank_base_url:
                    # Ops-level override: a true cross-encoder is served behind an
                    # OpenAI/Jina-style /v1/rerank endpoint. One batched call
                    # replaces per-doc prompt scoring; context config unchanged.
                    # Model resolution: an explicit, non-stale per-context
                    # reranker_model wins; otherwise fall back to the ops-level
                    # RERANK_MODEL (settings.rerank_model), then the built-in
                    # default (guards an explicitly-blanked RERANK_MODEL).
                    #
                    # "Stale" on the /v1/rerank endpoint also includes a
                    # self-hosted /v1/completions default model — an
                    # Ollama-registry id (the constant default or a configured
                    # SELF_HOSTED_RERANK_MODEL) that the vLLM/TEI rerank endpoint
                    # does not serve. The UI persists that id explicitly, so
                    # without re-resolving it the endpoint 404s and rerank
                    # silently dies for exactly the contexts this path targets.
                    vllm_stale_models = non_self_hosted_defaults | {
                        DEFAULT_SELF_HOSTED_RERANK_MODEL,
                        settings.self_hosted_rerank_model,
                    }
                    if not model or model in vllm_stale_models:
                        model = settings.rerank_model or DEFAULT_VLLM_RERANK_MODEL
                    logger.debug("using_vllm_reranker", user_id=user_id, model=model)
                    return VLLMReranker(
                        base_url=settings.rerank_base_url,
                        model=model,
                        api_key=settings.rerank_api_key or None,
                        prompt_format=settings.rerank_prompt_format,
                        instruction=settings.rerank_instruction,
                    )
                # Prompt-scoring path on SELF_HOSTED_BASE_URL. Re-resolve only a
                # stale REMOTE-provider default to the configurable self-hosted
                # default (SELF_HOSTED_RERANK_MODEL) — which a pure-vLLM stack
                # must override away from the Ollama-registry id so scoring does
                # not 404 to all-zero relevance.
                if not model or model in non_self_hosted_defaults:
                    model = settings.self_hosted_rerank_model or DEFAULT_SELF_HOSTED_RERANK_MODEL
                logger.debug("using_self_hosted_reranker", user_id=user_id, model=model)
                return SelfHostedReranker(
                    base_url=settings.self_hosted_base_url,
                    model=model,
                    api_key=settings.self_hosted_api_key or None,
                    prompt_format=settings.rerank_prompt_format,
                    instruction=settings.rerank_instruction,
                )

        # API-key providers (Voyage, Cohere).
        # Issue #385: workspace-keyed lookup; user_id is for audit, not a filter.
        # Without a workspace context the reranker is treated as not configured —
        # API-key reranker rows live in the external_api_keys table which is now
        # workspace-scoped (NOT NULL), so a no-workspace caller has nothing to find.
        if not workspace_id:
            logger.debug("no_reranker_configured", user_id=user_id, reason="no_workspace_context")
            return None

        # #1569: RESOLVE_STORED_BYOK_KEYS=false — the API-key rerankers exist
        # only as external_api_keys rows, so there is nothing left to resolve.
        from services.byok_resolution import stored_byok_keys_disabled

        if stored_byok_keys_disabled():
            logger.debug(
                "no_reranker_configured", user_id=user_id, reason="stored_byok_keys_disabled"
            )
            return None

        workspace_uuid = UUID(workspace_id) if isinstance(workspace_id, str) else workspace_id
        conditions = [
            ExternalAPIKey.workspace_id == workspace_uuid,
            ExternalAPIKey.provider.in_(RERANKER_PROVIDERS),
            ExternalAPIKey.enabled.is_(True),
        ]
        if context_id:
            context_uuid = UUID(context_id) if isinstance(context_id, str) else context_id
            # Context-scoped key wins; fall back to workspace-scoped (context_id IS NULL).
            conditions.append(
                or_(
                    ExternalAPIKey.context_id == context_uuid,
                    ExternalAPIKey.context_id.is_(None),
                )
            )
        else:
            conditions.append(ExternalAPIKey.context_id.is_(None))

        query = (
            select(ExternalAPIKey)
            .where(and_(*conditions))
            .order_by(ExternalAPIKey.context_id.desc().nulls_last())
            .limit(1)
        )

        result = await self.db.execute(query)
        api_key_entry = result.scalar_one_or_none()

        if not api_key_entry:
            logger.debug("no_reranker_configured", user_id=user_id, context_id=context_id)
            return None

        # Decrypt API key
        encryptor = get_encryptor()
        api_key = encryptor.decrypt(str(api_key_entry.encrypted_value))
        provider_name = str(api_key_entry.provider)

        # Load project search config for model selection (Issue #130)
        model_name = await self._get_reranker_model(context_id, provider_name)

        # Return appropriate provider with dynamic model
        if provider_name == "voyage":
            logger.debug("using_voyage_reranker", user_id=user_id, model=model_name)
            return VoyageReranker(api_key, model=model_name)
        elif provider_name == "cohere":
            logger.debug("using_cohere_reranker", user_id=user_id, model=model_name)
            return CohereReranker(api_key, model=model_name)

        logger.warning("unknown_reranker_provider", provider=provider_name)
        return None

    async def rerank(
        self,
        query: str,
        candidates: list[dict],
        user_id: str,
        k: int,
        context_id: str | None = None,
        workspace_id: str | None = None,  # NEW: Workspace ID (Issue #146)
        outcome: dict[str, Any] | None = None,
    ) -> list[dict]:
        """Rerank search results using active provider.

        Args:
            query: Search query
            candidates: Candidate results with 'payload' containing 'summary'
            user_id: User ID
            k: Number of top results to return
            context_id: Optional context ID (Issue #82)
            workspace_id: Optional workspace ID (Issue #146)
            outcome: Optional out-parameter; ``outcome["scored"]`` says whether
                a provider actually scored the candidates (#1989). ``False``
                when there was nothing to score or no active provider, in
                which case the candidates come back untouched.

        Returns:
            Reranked results or original candidates if no reranker available
        """
        if outcome is not None:
            outcome["scored"] = False
        if not candidates:
            return candidates

        provider = await self.get_active_provider(user_id, context_id, workspace_id)

        if not provider:
            return candidates

        # Prepare documents for reranking
        documents = [
            f"{c['payload']['summary']} {c['payload'].get('context_summary', '')}"
            for c in candidates
        ]

        # Every candidate is scored (the cut to k comes after) so the shared
        # no-signal guard below sees the whole list, for every provider (#2033).
        n = len(candidates)
        results = await provider.rerank(query, documents, n)

        if outcome is not None:
            outcome["scored"] = True
        if n >= 2 and len(results) == n:
            scores = {r["relevance_score"] for r in results}
            if scores == {0.0}:
                # The measured Qwen3-on-Ollama case: raise so SearchService
                # fails open (rerank_failed_open) and the hybrid scores stay.
                raise RerankNotDiscriminatingError(
                    f"{provider.provider_name} rerank scored all {n} candidates 0"
                )
            if len(scores) == 1:
                # Identical non-zero scores: a legitimate tie is possible on a
                # small list (two relevant docs at 1, duplicate memories), so
                # not a failure — but no ranking either. Keep the hybrid order
                # and scores rather than overwrite them with one constant.
                logger.info("rerank_no_signal", provider=provider.provider_name, doc_count=n)
                return candidates[:k]

        # Map reranked results back to candidates
        reranked = []
        for result in results[:k]:
            idx = result["index"]
            candidate = candidates[idx]
            candidate["rerank_score"] = result["relevance_score"]
            candidate["hybrid_score"] = result["relevance_score"]  # Override hybrid score
            reranked.append(candidate)

        logger.debug(
            "reranking_completed",
            provider=provider.provider_name,
            input_count=len(candidates),
            output_count=len(reranked),
        )

        return reranked

    async def _get_reranker_model(self, context_id: str | None, provider_name: str) -> str:
        """Get reranker model name from context configuration.

        Issue #130: Dynamic model selection based on context settings.

        Args:
            context_id: Context ID (string format)
            provider_name: Reranker provider name ('voyage' or 'cohere')

        Returns:
            Model name string (provider-specific)
        """
        if not context_id:
            model = default_reranker_model_for(provider_name)
            logger.debug(
                "reranker_model_default",
                provider=provider_name,
                model=model,
                reason="no_context_id",
            )
            return model

        # Load config from database
        try:
            repo = ContextSearchConfigRepository(self.db)
            config = await repo.create_or_get(UUID(context_id))

            # Verify provider matches config
            if config.reranker_provider != provider_name:
                logger.warning(
                    "reranker_provider_mismatch",
                    context_id=context_id,
                    config_provider=config.reranker_provider,
                    active_provider=provider_name,
                    fallback="using_config_model_anyway",
                )

            model = config.reranker_model or default_reranker_model_for(provider_name)
            logger.debug(
                "reranker_model_loaded",
                context_id=context_id,
                provider=provider_name,
                model=model,
            )
            return model

        except Exception as e:
            model = default_reranker_model_for(provider_name)
            logger.warning(
                "reranker_model_load_failed",
                context_id=context_id,
                provider=provider_name,
                error=str(e),
                fallback_model=model,
            )
            return model
