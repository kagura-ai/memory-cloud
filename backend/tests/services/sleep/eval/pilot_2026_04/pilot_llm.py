"""Pilot #249 — thin LLM wrappers for annotation.

Deliberately minimal because:

1. This is research code for a 1-day experiment, not production.
2. ``backend/src/services/llm_service.py`` is OpenAI/Ollama only — it has
   no path for Claude or Gemini. Extending it would require touching
   production code.
3. Adding ``anthropic`` / ``google-genai`` as permanent backend dependencies
   is overkill for a one-off pilot.

## Annotator design (changed during Phase B.2)

The original spec called for ``claude-opus-4-6 + gpt-4o`` as the two
annotators. During Phase B.2 setup we:

- Verified ``gpt-4o`` is **deprecated and removed** from the OpenAI API.
  Replaced with ``gpt-5.4`` (released 2026-03-05, current stable mainline).
- Discovered the local ``anthropic 0.37.1`` SDK is incompatible with the
  installed ``httpx`` version (``proxies`` keyword removed). Rather than
  upgrade the venv, we drop Claude from the default annotator set.
- Added ``gemini-2.5-pro`` as the second annotator. This is actually a
  **stronger** answer to the DS PhD review's concern about correlated
  raters (``claude + gpt`` share more training data than ``gpt + gemini``,
  which come from completely different organizations).

Default annotators: ``["openai", "gemini"]`` (gpt-5.4 + gemini-2.5-pro).
``claude`` is still wired up so a future operator with a working
``anthropic`` SDK can opt into a 3-way ensemble.

## Install requirements

In the operator's dev venv (NOT ``backend/pyproject.toml``)::

    pip install openai google-genai
    # optional, only if you want Claude annotation:
    pip install --upgrade anthropic        # 0.40+ for httpx compat

SDK imports are deferred to first call so this module can be imported
from places that don't need LLM access (e.g. the determinism test).

## Return shape

On success::

    {
        "parsed": {"label": "...", "rationale": "...", "confidence": 0.0..1.0},
        "tokens": int,       # total tokens billed for this call
        "raw": str,          # raw text response from the model
        "model": str,        # exact model id the provider returned
    }

On error::

    {
        "error": str,        # one-line error description
        "tokens": 0,
        "raw": "",
        "model": str,
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Literal

logger = logging.getLogger(__name__)

Annotator = Literal["claude", "openai", "gemini"]
DEFAULT_ANNOTATORS: tuple[Annotator, ...] = ("openai", "gemini")

# Model ids. If a model is deprecated, bump here and record in the snapshot.
# All three are pinned to mainline non-preview variants for reproducibility.
CLAUDE_MODEL = "claude-opus-4-6"  # requires anthropic >= 0.40
OPENAI_MODEL = "gpt-5.4"  # released 2026-03-05; gpt-4o is deprecated
GEMINI_MODEL = "gemini-2.5-pro"  # latest stable; gemini-3-pro is preview

# Per-call output budget. Set high enough to cover Gemini 2.5's "thinking"
# tokens (capped at GEMINI_THINKING_BUDGET below) PLUS the visible JSON
# output (~80). 1000 leaves ~488 tokens for output after thinking, which
# is comfortable for the small JSON we expect.
MAX_TOKENS_OUT = 1000

# Gemini 2.5 mainline models always run in "thinking" mode — they will
# scale thinking up to consume the entire output budget unless we cap
# them. ``thinking_budget=0`` is rejected by gemini-2.5-pro ("This model
# only works in thinking mode"). 512 is enough for the 6-class label task
# in our smoke tests; increase if quality regresses.
GEMINI_THINKING_BUDGET = 512

TEMPERATURE = 0.1

# On transient errors (rate limit, network), retry once after this delay.
RETRY_DELAY_SEC = 2.0


def _strip_code_fences(text: str) -> str:
    """Remove ``` or ```json fences models sometimes wrap JSON in."""
    text = text.strip()
    if not text.startswith("```"):
        return text
    try:
        text = text.split("\n", 1)[1]
    except IndexError:
        return text
    text = text.rsplit("```", 1)[0]
    return text.strip()


def _parse_json_response(raw: str) -> dict[str, Any]:
    """Best-effort JSON extraction from an LLM response."""
    stripped = _strip_code_fences(raw)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            return json.loads(stripped[start : end + 1])
        raise


# ----------------------------------------------------------------------------
# Provider wrappers
# ----------------------------------------------------------------------------


async def _judge_claude(system_prompt: str, user_msg: str) -> dict[str, Any]:
    from anthropic import AsyncAnthropic  # type: ignore[import-not-found]

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {
            "error": "ANTHROPIC_API_KEY not set in environment",
            "tokens": 0,
            "raw": "",
            "model": CLAUDE_MODEL,
        }

    for attempt in (1, 2):
        try:
            client = AsyncAnthropic()
            resp = await client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=MAX_TOKENS_OUT,
                temperature=TEMPERATURE,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(block.text for block in resp.content if hasattr(block, "text"))
            tokens = (getattr(resp.usage, "input_tokens", 0) or 0) + (
                getattr(resp.usage, "output_tokens", 0) or 0
            )
            return {
                "parsed": _parse_json_response(text),
                "tokens": tokens,
                "raw": text,
                "model": resp.model or CLAUDE_MODEL,
            }
        except Exception as exc:
            logger.warning("claude_attempt_%s_failed: %s", attempt, exc)
            if attempt == 1:
                await asyncio.sleep(RETRY_DELAY_SEC)
                continue
            return {
                "error": f"{type(exc).__name__}: {exc}",
                "tokens": 0,
                "raw": "",
                "model": CLAUDE_MODEL,
            }


async def _judge_openai(system_prompt: str, user_msg: str) -> dict[str, Any]:
    from openai import AsyncOpenAI  # type: ignore[import-not-found]

    if not os.environ.get("OPENAI_API_KEY"):
        return {
            "error": "OPENAI_API_KEY not set in environment",
            "tokens": 0,
            "raw": "",
            "model": OPENAI_MODEL,
        }

    for attempt in (1, 2):
        try:
            client = AsyncOpenAI()
            resp = await client.chat.completions.create(
                model=OPENAI_MODEL,
                temperature=TEMPERATURE,
                # NOTE: gpt-5+ uses ``max_completion_tokens``, not ``max_tokens``.
                max_completion_tokens=MAX_TOKENS_OUT,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
            )
            text = resp.choices[0].message.content or ""
            tokens = getattr(resp.usage, "total_tokens", 0) or 0
            return {
                "parsed": _parse_json_response(text),
                "tokens": tokens,
                "raw": text,
                "model": resp.model or OPENAI_MODEL,
            }
        except Exception as exc:
            logger.warning("openai_attempt_%s_failed: %s", attempt, exc)
            if attempt == 1:
                await asyncio.sleep(RETRY_DELAY_SEC)
                continue
            return {
                "error": f"{type(exc).__name__}: {exc}",
                "tokens": 0,
                "raw": "",
                "model": OPENAI_MODEL,
            }


async def _judge_gemini(system_prompt: str, user_msg: str) -> dict[str, Any]:
    """Use the modern ``google-genai`` SDK (1.0+).

    The legacy ``google-generativeai`` package is broken in this venv
    due to a proto-plus / google._upb._message compat issue, so we
    deliberately import the new SDK only.
    """
    from google import genai  # type: ignore[import-not-found]
    from google.genai import types as genai_types  # type: ignore[import-not-found]

    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        return {
            "error": "GEMINI_API_KEY (or GOOGLE_API_KEY) not set in environment",
            "tokens": 0,
            "raw": "",
            "model": GEMINI_MODEL,
        }

    for attempt in (1, 2):
        try:
            client = genai.Client()  # auto-picks GEMINI_API_KEY / GOOGLE_API_KEY
            # Gemini 2.5 produces "thinking" tokens; max_output_tokens must
            # cover both thinking + visible output. MAX_TOKENS_OUT=500 gives
            # comfortable headroom for the small JSON we expect.
            config = genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=TEMPERATURE,
                max_output_tokens=MAX_TOKENS_OUT,
                response_mime_type="application/json",
                thinking_config=genai_types.ThinkingConfig(
                    thinking_budget=GEMINI_THINKING_BUDGET,
                ),
            )
            resp = await asyncio.to_thread(
                client.models.generate_content,
                model=GEMINI_MODEL,
                contents=user_msg,
                config=config,
            )
            text = resp.text or ""
            usage = resp.usage_metadata
            tokens = getattr(usage, "total_token_count", 0) if usage else 0
            return {
                "parsed": _parse_json_response(text),
                "tokens": int(tokens or 0),
                "raw": text,
                "model": getattr(resp, "model_version", None) or GEMINI_MODEL,
            }
        except Exception as exc:
            logger.warning("gemini_attempt_%s_failed: %s", attempt, exc)
            if attempt == 1:
                await asyncio.sleep(RETRY_DELAY_SEC)
                continue
            return {
                "error": f"{type(exc).__name__}: {exc}",
                "tokens": 0,
                "raw": "",
                "model": GEMINI_MODEL,
            }


async def judge_pair(
    annotator: Annotator,
    system_prompt: str,
    user_msg: str,
) -> dict[str, Any]:
    """Dispatch to the right annotator and return a normalized dict.

    On success::

        {"parsed": {...}, "tokens": int, "raw": str, "model": str}

    On failure::

        {"error": str, "tokens": 0, "raw": "", "model": str}
    """
    if annotator == "claude":
        return await _judge_claude(system_prompt, user_msg)
    if annotator == "openai":
        return await _judge_openai(system_prompt, user_msg)
    # annotator is Literal; the remaining branch is "gemini"
    return await _judge_gemini(system_prompt, user_msg)


def format_user_msg(row: dict[str, Any]) -> str:
    """Format a pair row into the user-message shape the labeling prompt expects.

    Must exactly match the 'Input format' section of labeling_prompt.md, or
    the annotator outputs become incomparable across runs.
    """
    return (
        f'src.summary: "{row["src_summary"]}"\n'
        f"src.tags: {list(row.get('src_tags') or [])}\n"
        f'dst.summary: "{row["dst_summary"]}"\n'
        f"dst.tags: {list(row.get('dst_tags') or [])}\n"
        f"cosine: {row['cosine_similarity']:.3f}"
    )
