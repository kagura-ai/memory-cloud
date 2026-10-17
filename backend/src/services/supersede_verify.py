"""Verification of a supersede suggestion before it is surfaced (#2016).

The #1403 detector proposes a ``supersede_candidate`` whenever a new memory's
nearest neighbour scores at or above ``_SUPERSEDE_SUGGEST_THRESHOLD``. Cosine
alone cannot tell "a new value of the same fact" from "the same kind of fact
about a different subject": on the frozen eval slice it proposed a supersede
for every true update AND for every sibling-entity negative, and accepting
every suggestion hid half of the live facts.

This module adds the judgement that similarity cannot make. With
``supersede_verify_mode="llm"`` the Sleep judge (same provider / model /
credential lane as Sleep Maintenance) is asked, one pair per call, whether the
NEW memory replaces the OLD one or whether they are parallel facts. The
verdict is stored on the candidate under ``verification``:

    {"memory_id": ..., "similarity": ..., "detected_at": ...,
     "verification": {"verdict": "supersedes" | "parallel" | "unclear",
                      "confidence": 0.0-1.0, "reason": "...", "model": "...",
                      "at": "...", "verified": true | false}}

* ``parallel`` — the suggestion is kept in the column (so a mechanical
  re-embed does not re-judge the pair) but never surfaced.
* ``supersedes`` with ``confidence >= supersede_verify_min_confidence`` —
  surfaced with ``verified: true``, the signal a client may auto-accept on.
  The floor is applied when the candidate is READ, so raising it later
  applies to stored verdicts too; the stored ``verified`` is audit only.
* anything else (``unclear``, a low-confidence ``supersedes``, the judge off,
  over its daily cap, its counter unreachable, or failing) — surfaced as
  before, without ``verified``.

A verdict is about two texts. It carries a ``fingerprint`` of both (summary
and content head) and an ``old_fingerprint`` of the old side alone, so an
edit of either memory re-judges the pair instead of reusing the verdict, and
the read path sets a verdict aside once the old memory's text no longer
matches what the judge saw (``verification_stale``). Text, not
``updated_at``: Sleep stamps ``updated_at`` for importance and tag changes
that the judge never looked at.

Every billed judge call leaves one ``llm_call_log`` row (#2019,
``caller='supersede_verify'``, ``call_type='completion'``): tokens, the
write-time cost snapshot, ``paid_by`` from the credential the call actually
used, and the verdict in ``call_metadata``. The row goes through its own
short session, so it lands even when the ingest path returns early and a
ledger failure never touches the embedding task's session. A call that
raised (no answer, nothing billed that we can count) writes no row; an
unparseable answer does, since its tokens were spent.

The judge never creates an edge: the #1208 over-supersede prevention keeps
edge authorship with the user / agent. Memory text goes into the prompt only
between the untrusted-content markers Sleep uses (#919); trusted metadata
(type, created_at) stays outside them.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from services.sleep.judge_lane import judge_platform_only
from services.sleep.prompts import INJECTION_RESISTANCE_DIRECTIVE, wrap_untrusted_content
from services.supersede_dismissal import RESURFACE_SIMILARITY_DELTA
from utils.datetime import to_utc_iso, utcnow
from utils.logger import get_logger

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from neural.config import NeuralMemoryConfig

logger = get_logger(__name__)

VERIFY_MODE_OFF = "off"
VERIFY_MODE_LLM = "llm"
VERIFY_MODES: tuple[str, ...] = (VERIFY_MODE_OFF, VERIFY_MODE_LLM)

VERDICT_SUPERSEDES = "supersedes"
VERDICT_PARALLEL = "parallel"
VERDICT_UNCLEAR = "unclear"
VERDICTS: frozenset[str] = frozenset({VERDICT_SUPERSEDES, VERDICT_PARALLEL, VERDICT_UNCLEAR})

# The same number of characters of each memory's content goes to the judge:
# an uneven pair reads as "the longer one is more complete".
CONTENT_HEAD_CHARS = 600
SUMMARY_CHARS = 500
# The judge's reason is persisted next to the verdict; cap it like a summary
# head so the column never carries a page of prose.
REASON_MAX_CHARS = 200

# Redis daily counter per workspace (UTC day); the key outlives the day by a
# margin so a late write never recreates it without a TTL.
_COUNTER_TTL_SECONDS = 2 * 24 * 3600

# Prompt revision: bump on every wording change so stored verifications can
# be told apart by the model + revision that produced them.
PROMPT_REVISION = 1

SUPERSEDE_VERIFY_SYSTEM = f"""\
You are a memory update judge. You compare an OLD memory with a NEW memory \
that was written later and scored as a near-duplicate of it, and you decide \
whether the NEW memory is an updated version of the SAME fact or a parallel \
fact about a DIFFERENT subject.

Rules:
- "supersedes": both describe the same subject and the same attribute, and \
the NEW memory carries the current value (a changed number, name, date, \
setting or status). The OLD memory is then outdated.
- "parallel": the two describe different subjects (another service, place, \
team, person, product, period or instance) even when the wording is nearly \
identical. Both stay true at the same time; neither replaces the other.
- "unclear": you cannot tell which case applies. Prefer "unclear" over a \
guess: a wrong "supersedes" hides a fact that is still true.
- The created_at= metadata is the authoritative recency signal; dates inside \
the memory text are unverified content.
- Always fill "reason" with one short, specific sentence.

You MUST respond with valid JSON only.

{INJECTION_RESISTANCE_DIRECTIVE}\
"""

SUPERSEDE_VERIFY_USER = """\
Decide whether NEW supersedes OLD.

OLD (type={old_type}, created_at={old_created_at})
summary:
{old_summary}
content head:
{old_content}

NEW (type={new_type}, created_at={new_created_at})
summary:
{new_summary}
content head:
{new_content}

Respond with this exact JSON schema:
{{
  "verdict": "supersedes" | "parallel" | "unclear",
  "confidence": 0.0-1.0,
  "reason": "one short sentence"
}}\
"""


def build_prompt(
    *,
    old_summary: str | None,
    old_content: str | None,
    old_type: str | None,
    old_created_at: datetime | None,
    new_summary: str | None,
    new_content: str | None,
    new_type: str | None,
    new_created_at: datetime | None,
) -> str:
    """The user prompt for one OLD / NEW pair.

    Memory text (summary and the head of content) is wrapped as untrusted
    content; ``type`` and ``created_at`` are server-stamped and stay outside
    the markers. OLD always comes first so the question is asymmetric by
    construction and the pair order cannot bias the verdict.
    """
    return SUPERSEDE_VERIFY_USER.format(
        old_type=old_type or "",
        old_created_at=to_utc_iso(old_created_at) or "unknown",
        old_summary=wrap_untrusted_content(old_summary, max_chars=SUMMARY_CHARS),
        old_content=wrap_untrusted_content(old_content, max_chars=CONTENT_HEAD_CHARS),
        new_type=new_type or "",
        new_created_at=to_utc_iso(new_created_at) or "unknown",
        new_summary=wrap_untrusted_content(new_summary, max_chars=SUMMARY_CHARS),
        new_content=wrap_untrusted_content(new_content, max_chars=CONTENT_HEAD_CHARS),
    )


def parse_judgement(parsed: Any) -> tuple[str, float, str] | None:
    """Validate the judge's JSON; ``None`` when it is not usable.

    Returns ``(verdict, confidence, reason)`` with the verdict one of
    :data:`VERDICTS`, the confidence clamped to ``[0, 1]`` and the reason cut
    to :data:`REASON_MAX_CHARS`.
    """
    if not isinstance(parsed, dict):
        return None
    verdict = parsed.get("verdict")
    if not isinstance(verdict, str):
        return None
    verdict = verdict.strip().lower()
    if verdict not in VERDICTS:
        return None
    raw_confidence = parsed.get("confidence")
    if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
        return None
    confidence = float(raw_confidence)
    if confidence != confidence:  # NaN
        return None
    confidence = min(1.0, max(0.0, confidence))
    reason = parsed.get("reason")
    reason_text = reason.strip()[:REASON_MAX_CHARS] if isinstance(reason, str) else ""
    return verdict, confidence, reason_text


def text_fingerprint(summary: str | None, content_head: str | None) -> str:
    """A short digest of one memory's text as the judge sees it.

    Covers what :func:`build_prompt` sends for one side: the summary and the
    first :data:`CONTENT_HEAD_CHARS` of the content. The read path computes
    the same digest from ``summary`` and ``substr(content, 1, 600)``.
    """
    parts = ((summary or "")[:SUMMARY_CHARS], (content_head or "")[:CONTENT_HEAD_CHARS])
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:16]


def pair_fingerprint(old: Any, new: Any) -> str:
    """A short digest of the two texts the judge saw (both sides)."""
    joined = text_fingerprint(old.summary, old.content) + text_fingerprint(new.summary, new.content)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def min_confidence_from_env(default: float = 0.7) -> float:
    """``SUPERSEDE_VERIFY_MIN_CONFIDENCE`` as the read path uses it.

    A single-field read that never raises: the resolver runs on every recall
    / reference and must not inherit ``NeuralMemoryConfig.from_env()``'s
    validation of fifty unrelated knobs. An unparseable or out-of-range value
    falls back to ``default``.
    """
    raw = os.getenv("SUPERSEDE_VERIFY_MIN_CONFIDENCE")
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value != value or not (0.0 <= value <= 1.0):
        return default
    return value


def verification_record(
    verdict: str,
    confidence: float,
    reason: str,
    *,
    model: str,
    min_confidence: float,
    fingerprint: str,
    old_fingerprint: str,
    at: str | None = None,
) -> dict[str, Any]:
    """The value stored under ``supersede_candidate.verification``.

    ``verified`` here is the floor at judge time, kept for audit; the read
    path recomputes it against the current floor (:func:`is_verified`).
    ``at`` is when the texts were read (before the call), not when the
    answer came back.
    """
    return {
        "verdict": verdict,
        "confidence": round(confidence, 4),
        "reason": reason,
        "model": model,
        "prompt_revision": PROMPT_REVISION,
        "fingerprint": fingerprint,
        "old_fingerprint": old_fingerprint,
        "at": at or to_utc_iso(utcnow()),
        "verified": verdict == VERDICT_SUPERSEDES and confidence >= min_confidence,
    }


def stored_verification(candidate: Any) -> dict[str, Any] | None:
    """The verification dict of a stored candidate, or ``None``."""
    if not isinstance(candidate, dict):
        return None
    verification = candidate.get("verification")
    if not isinstance(verification, dict):
        return None
    verdict = verification.get("verdict")
    if not isinstance(verdict, str) or verdict not in VERDICTS:
        return None
    return verification


def is_parallel(candidate: Any) -> bool:
    """Whether a stored candidate was judged a parallel fact (never surfaced)."""
    verification = stored_verification(candidate)
    return verification is not None and verification.get("verdict") == VERDICT_PARALLEL


def is_verified(candidate: Any, *, min_confidence: float) -> bool:
    """Whether a stored candidate carries a confirming verdict at or above
    the CURRENT floor — the signal a client may act on without reading both."""
    verification = stored_verification(candidate)
    if verification is None or verification.get("verdict") != VERDICT_SUPERSEDES:
        return False
    confidence = verification.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return False
    return float(confidence) >= min_confidence


def verification_stale(
    candidate: Any, *, old_summary: str | None, old_content_head: str | None
) -> bool:
    """Whether the OLD memory's text is no longer what the judge saw.

    The verdict was given about a specific old text; once that text differs
    (``old_fingerprint`` no longer matches), the verdict is about text that no
    longer exists, so neither ``verified`` nor the parallel suppression may
    rest on it. A verdict without an ``old_fingerprint`` is stale. The next
    re-embed of the new memory re-judges the pair (its pair fingerprint moved
    too).
    """
    verification = stored_verification(candidate)
    if verification is None:
        return False
    return verification.get("old_fingerprint") != text_fingerprint(old_summary, old_content_head)


def reuse_verification(
    stored: Any, *, target_id: str, similarity: float, fingerprint: str
) -> dict[str, Any] | None:
    """An earlier verdict for the same pair, when the pair has not moved.

    ``process_pending_embedding`` also runs for Sleep reindexes and backfills,
    which recompute the same vector; re-judging on each of those would spend
    an LLM call to learn what the column already says. The pair is "the same"
    when the stored candidate names the same target, the similarity is within
    :data:`RESURFACE_SIMILARITY_DELTA` of the stored one (the rule the
    dismissal tombstone uses) AND the texts the judge saw are unchanged
    (``fingerprint``) — cosine alone cannot tell an edit that flips the
    verdict, which is the whole reason the judge exists.
    """
    verification = stored_verification(stored)
    if verification is None or not isinstance(stored, dict):
        return None
    if str(stored.get("memory_id")) != target_id:
        return None
    if verification.get("fingerprint") != fingerprint:
        return None
    previous = stored.get("similarity")
    if not isinstance(previous, (int, float)):
        return None
    if abs(float(previous) - similarity) >= RESURFACE_SIMILARITY_DELTA:
        return None
    return verification


def counter_key(workspace_id: str) -> str:
    """Redis key of the workspace's judge-call counter for the current UTC day."""
    return f"supersede_verify:{workspace_id}:{utcnow().strftime('%Y-%m-%d')}"


# ``llm_call_log.caller`` for the judge's ledger rows (#2019).
LEDGER_CALLER = "supersede_verify"


@asynccontextmanager
async def _ledger_session(db: AsyncSession) -> AsyncIterator[AsyncSession]:
    """A fresh session on ``db``'s engine for the ledger write (#1985 pattern).

    The ledger row must land whatever the ingest path does next (it may return
    without committing), and a failed ledger write must not leave the
    embedding task's session needing a rollback (which would expire the
    memory it is still working on).
    """
    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

    bind = getattr(db, "bind", None)
    if isinstance(bind, AsyncEngine):
        session = _AsyncSession(bind=bind, expire_on_commit=False)
    else:
        from db.base import _get_session_factory

        session = _get_session_factory()()
    async with session:
        yield session


async def _record_judge_call(
    db: AsyncSession,
    response: Any,
    *,
    paid_by: str,
    user_id: str,
    workspace_id: str,
    context_id: str,
    call_metadata: dict[str, Any],
) -> None:
    """Write the judge call's ``llm_call_log`` row; never raises.

    Fail-open like the recall ledger: a lost row is an observability gap, not
    a reason to drop the verdict. ``call_metadata`` carries the verdict only,
    never memory text.
    """
    from services.llm_call_log_writer import LLMCallLogWriter

    try:
        async with _ledger_session(db) as session:
            row = await LLMCallLogWriter(session).record(
                caller=LEDGER_CALLER,
                call_type="completion",
                provider=response.provider,
                model=response.model,
                user_id=user_id,
                workspace_id=workspace_id,
                context_id=context_id,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cached_input_tokens=response.cached_input_tokens,
                cache_write_tokens=response.cache_write_tokens,
                paid_by=paid_by,
                call_metadata=call_metadata,
                fail_on_error=False,
            )
            if row is not None:
                await session.commit()
    except Exception as exc:  # noqa: BLE001 — never raises into the embedding task
        logger.warning(
            "supersede_verify_ledger_failed",
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
        )


# Workspaces whose cap was already reported today (reset when the UTC day
# changes, so the set never grows past the day's capped workspaces).
_CAP_REPORTED_DAY = ""
_CAP_REPORTED: set[str] = set()
_NO_PROVIDER_REPORTED = False


async def _under_daily_cap(workspace_id: str, max_calls: int) -> bool:
    """Count this call against the workspace's daily cap.

    Fail-closed: when the counter cannot be read the paid call is skipped and
    the candidate surfaces unverified (the pre-#2016 shape) — an outage must
    not turn into unbounded judge spend. Ingest itself is unaffected.
    """
    from db.redis import incrby_counter

    try:
        count = await incrby_counter(counter_key(workspace_id), 1, ttl=_COUNTER_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — never raises into the embedding task
        logger.warning(
            "supersede_verify_counter_failed",
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            hint="judge skipped; the candidate surfaces unverified",
        )
        return False
    if count <= max_calls:
        return True
    global _CAP_REPORTED_DAY
    today = utcnow().strftime("%Y-%m-%d")
    if today != _CAP_REPORTED_DAY:
        _CAP_REPORTED_DAY = today
        _CAP_REPORTED.clear()
    if workspace_id not in _CAP_REPORTED:
        _CAP_REPORTED.add(workspace_id)
        logger.warning(
            "supersede_verify_cap_reached",
            workspace_id=workspace_id,
            max_calls_per_day=max_calls,
            hint="suggestions surface unverified until the next UTC day",
        )
    return False


async def judge_pair(
    db: AsyncSession,
    *,
    config: NeuralMemoryConfig,
    user_id: str,
    workspace_id: str,
    context_id: str,
    new: Any,
    old: Any,
) -> dict[str, Any] | None:
    """Ask the Sleep judge whether ``new`` supersedes ``old``.

    Returns the verification record to store, or ``None`` when no verdict was
    obtained (no provider configured, daily cap reached, call or parse
    failure) — the caller then stores the candidate unverified, exactly as
    before #2016. Nothing here raises: this runs in the background embedding
    task and must never affect memory creation.

    Args:
        db: Session for the LLM service's credential lookup.
        config: The resolved ``NeuralMemoryConfig`` (``sleep_llm_*`` pair,
            ``supersede_verify_*`` fields).
        user_id: The writing user (audit only, as for Sleep).
        workspace_id: Workspace of both memories (credential scope, cap key).
        context_id: Context of both memories (credential scope).
        new: The freshly embedded memory.
        old: The candidate it may supersede.
    """
    global _NO_PROVIDER_REPORTED

    from services.llm_service import LLMService

    provider = config.sleep_llm_provider
    if not provider:
        if not _NO_PROVIDER_REPORTED:
            _NO_PROVIDER_REPORTED = True
            logger.warning(
                "supersede_verify_no_provider",
                hint="SUPERSEDE_VERIFY_MODE=llm needs SLEEP_LLM_PROVIDER; suggestions "
                "surface unverified",
            )
        return None

    if not await _under_daily_cap(workspace_id, config.supersede_verify_max_calls_per_day):
        return None

    # Stamp the verdict with the moment the texts were read, not the moment
    # the answer came back: an edit during the round trip must read as later.
    read_at = to_utc_iso(utcnow())
    prompt = build_prompt(
        old_summary=old.summary,
        old_content=old.content,
        old_type=old.type,
        old_created_at=old.created_at,
        new_summary=new.summary,
        new_content=new.content,
        new_type=new.type,
        new_created_at=new.created_at,
    )
    llm = LLMService(db)
    try:
        response = await llm.complete_json(
            user_id=user_id,
            prompt=prompt,
            system_prompt=SUPERSEDE_VERIFY_SYSTEM,
            context_id=context_id,
            workspace_id=workspace_id,
            model=config.sleep_llm_model,
            provider=provider,
            max_tokens=512,
            platform_only=judge_platform_only(config),
        )
    except Exception as exc:  # noqa: BLE001 — best-effort, never blocks ingest
        logger.warning(
            "supersede_verify_failed",
            memory_id=str(new.id),
            candidate_memory_id=str(old.id),
            provider=provider,
            model=config.sleep_llm_model,
            error_type=type(exc).__name__,
        )
        return None

    ledger = {
        "paid_by": llm.resolve_paid_by(),
        "user_id": user_id,
        "workspace_id": workspace_id,
        "context_id": context_id,
    }
    judgement = parse_judgement(response.parsed)
    if judgement is None:
        await _record_judge_call(
            db,
            response,
            call_metadata={"outcome": "unparseable", "prompt_revision": PROMPT_REVISION},
            **ledger,
        )
        logger.warning(
            "supersede_verify_unparseable",
            memory_id=str(new.id),
            candidate_memory_id=str(old.id),
            provider=response.provider,
            model=response.model,
        )
        return None
    verdict, confidence, reason = judgement
    record = verification_record(
        verdict,
        confidence,
        reason,
        model=response.model,
        min_confidence=config.supersede_verify_min_confidence,
        fingerprint=pair_fingerprint(old, new),
        old_fingerprint=text_fingerprint(old.summary, old.content),
        at=read_at,
    )
    await _record_judge_call(
        db,
        response,
        call_metadata={
            "verdict": verdict,
            "confidence": record["confidence"],
            "verified": record["verified"],
            "prompt_revision": PROMPT_REVISION,
        },
        **ledger,
    )
    logger.info(
        "supersede_verify_judged",
        memory_id=str(new.id),
        candidate_memory_id=str(old.id),
        verdict=verdict,
        confidence=record["confidence"],
        verified=record["verified"],
        provider=response.provider,
        model=response.model,
        tokens=response.total_tokens,
    )
    return record
