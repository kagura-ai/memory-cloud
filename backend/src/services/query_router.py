"""Deterministic query-intent classifier for retrieval routing (#1212).

Pre-registered eval result (F1/H1): at 300-document scale fixed-weight hybrid
fusion is a null (negative at the 4b embedder) against its own dense
component, but the per-bucket decomposition is design-consistent — keyword
(BM25 + Sudachi) wins exact-match and hiragana-heavy buckets, semantic wins
semantic/cross-source buckets. The lanes exist (``search_mode``); this module
supplies the missing per-query lane choice.

Design constraints (issue #1212, gate1):

- **Pure function, no I/O, no LLM** on the hot path — precompiled regex and
  character-class counting only; sub-millisecond per call.
- **Deterministic**: the same query always yields the same route, so
  ``log_only`` telemetry is replayable and the (future, stage-3) calibration
  gate can evaluate the exact production classifier offline.
- Thresholds below are Phase-1 priors grounded in the eval bucket
  decomposition, NOT calibrated constants — stage 3 (frozen-corpus
  calibration gate) owns re-tuning them before any default flip.

Rules, in order:

1. Keyword signal (quoted literal / exact ID / code symbol) with little
   natural-language remainder → ``keyword``.
2. Keyword signal + substantial natural-language remainder (genuinely mixed)
   → ``hybrid``.
3. Hiragana-dominant query → ``keyword`` (the Sudachi lane; embedding models
   underperform on hiragana-heavy text).
4. Everything else → ``semantic`` (the strongest single arm at both measured
   capacities).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --- Phase-1 priors (stage-3 calibration owns re-tuning; see module docstring)

# A query is "hiragana-dominant" when hiragana makes up more than this share
# of its script-bearing characters (hiragana/katakana/kanji/latin/digits).
HIRAGANA_DOMINANCE_THRESHOLD = 0.5
# Dominance is only meaningful with at least this many script-bearing chars.
MIN_SCRIPT_CHARS = 4
# A keyword-signal query with at least this many natural-language tokens left
# after removing the signal spans is "genuinely mixed" → hybrid.
MIXED_NATURAL_TOKEN_THRESHOLD = 4
# Classification reads at most this many characters. Routing signals live at
# the front of real queries, and the bound makes the remainder-stripping loop
# (O(matches × length)) constant-bounded — an attacker-sized query cannot
# turn the hot-path classifier into an algorithmic-DoS vector (gate2/CSO).
CLASSIFY_MAX_CHARS = 2000

# Single-quote literals must not butt against a word character on either
# side, so apostrophes in ordinary English (contractions, possessives —
# "what's the user's role") never read as an opening quote and hijack the
# semantic lane. Word-boundary-style assertions (not whitespace-only) keep
# punctuation-adjacent literals detectable — "what is 'foo bar'?" and
# "('foo bar')" count like their double-quote twins. No embedded ^ (CodeQL
# flags an alternation-embedded start-anchor as unmatchable).
_QUOTED_LITERAL = re.compile(r'"[^"]{2,}"' r"|(?<![\w'])'[^']{2,}'(?![\w'])" r"|`[^`]{2,}`")
_EXACT_ID_PATTERNS = (
    # UUID
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    # long hex (commit SHAs, hashes) — must contain a digit to avoid words
    re.compile(r"\b(?=[0-9a-f]*[0-9])[0-9a-f]{8,40}\b"),
    # issue / PR references
    re.compile(r"(?:^|\s)#\d+\b"),
    # version strings
    re.compile(r"\bv\d+\.\d+(?:\.\d+)?\b"),
    # error / ticket codes (HEALTH-001, CWE-639, HTTP-500)
    re.compile(r"\b[A-Z][A-Z0-9]{1,15}-\d+\b"),
    # long digit runs (timestamps, ids)
    re.compile(r"\b\d{6,}\b"),
)
_CODE_SYMBOL_PATTERNS = (
    # snake_case identifiers
    re.compile(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b"),
    # dotted paths / filenames (resolve.config.path, main.py)
    re.compile(r"\b\w+\.(?:\w+\.)*(?:py|ts|tsx|js|jsx|md|json|yml|yaml|toml|sql|sh)\b"),
    re.compile(r"\b\w+(?:\.\w+){2,}\b"),
    # camelCase (lower→upper transition inside a word)
    re.compile(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b"),
    # PascalCase with >=3 segments (MemoryHealthService). Two-segment words
    # (JavaScript, GitHub) stay off this lane — they are ordinary proper
    # nouns in natural questions far more often than code symbols.
    re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+){2,}\b"),
    # call syntax / scope operators / paths
    re.compile(r"\w+\(\)|::|->|(?:^|\s)/[\w.-]+(?:/[\w.-]+)+\b"),
)

_HIRAGANA = re.compile(r"[ぁ-ゟ]")
_KATAKANA = re.compile(r"[゠-ヿ]")
_KANJI = re.compile(r"[一-鿿]")
_LATIN_ALNUM = re.compile(r"[A-Za-z0-9]")
# Natural-language remainder tokens: latin words of >=3 letters, or CJK runs
# of >=2 chars (a rough content-word proxy that works for both scripts).
_NATURAL_TOKEN = re.compile(r"[A-Za-z]{3,}|[ぁ-ヿ一-鿿]{2,}")

LANE_KEYWORD = "keyword"
LANE_SEMANTIC = "semantic"
LANE_HYBRID = "hybrid"


@dataclass(frozen=True)
class QueryRoute:
    """Deterministic routing decision for one query.

    Attributes:
        lane: The chosen search lane (keyword / semantic / hybrid).
        reasons: Ordered, human-readable rule hits that produced the lane.
        features: Numeric signals backing the decision — safe to log
            (contains counts and ratios, never query text).
    """

    lane: str
    reasons: tuple[str, ...] = ()
    features: dict[str, Any] = field(default_factory=dict)


def classify_query(query: str) -> QueryRoute:
    """Classify a recall query into a search lane. Pure and deterministic.

    Args:
        query: The raw recall query string.

    Returns:
        QueryRoute with the chosen lane, rule-hit reasons, and loggable
        numeric features (no query text).
    """
    text = query[:CLASSIFY_MAX_CHARS].strip()

    quoted = _QUOTED_LITERAL.findall(text)
    exact_ids: list[str] = []
    for pat in _EXACT_ID_PATTERNS:
        exact_ids.extend(m if isinstance(m, str) else m[0] for m in pat.findall(text))
    code_symbols: list[str] = []
    for pat in _CODE_SYMBOL_PATTERNS:
        code_symbols.extend(m if isinstance(m, str) else m[0] for m in pat.findall(text))

    hiragana = len(_HIRAGANA.findall(text))
    katakana = len(_KATAKANA.findall(text))
    kanji = len(_KANJI.findall(text))
    latin = len(_LATIN_ALNUM.findall(text))
    script_chars = hiragana + katakana + kanji + latin
    hiragana_ratio = (hiragana / script_chars) if script_chars else 0.0

    # Natural-language remainder: strip every matched signal span, count what
    # is left. Signals stripped as literal substrings — good enough for a
    # token-count proxy.
    # Strip longest spans first: a snake_case identifier is often a strict
    # substring of a dotted path from the same query — removing the shorter
    # span first would break the longer one and leak its outer fragments
    # back in as false "natural language" tokens.
    remainder = text
    for span in sorted((*quoted, *exact_ids, *code_symbols), key=len, reverse=True):
        remainder = remainder.replace(span.strip(), " ")
    natural_tokens = len(_NATURAL_TOKEN.findall(remainder))

    features: dict[str, Any] = {
        "quoted_literals": len(quoted),
        "exact_id_hits": len(exact_ids),
        "code_symbol_hits": len(code_symbols),
        "hiragana_ratio": round(hiragana_ratio, 3),
        "script_chars": script_chars,
        "natural_tokens": natural_tokens,
        "query_len": len(text),
    }

    reasons: list[str] = []
    if quoted:
        reasons.append("quoted_literal")
    if exact_ids:
        reasons.append("exact_id")
    if code_symbols:
        reasons.append("code_symbol")

    if reasons:  # keyword signal present
        if natural_tokens >= MIXED_NATURAL_TOKEN_THRESHOLD:
            reasons.append("mixed_natural_language")
            return QueryRoute(LANE_HYBRID, tuple(reasons), features)
        return QueryRoute(LANE_KEYWORD, tuple(reasons), features)

    if script_chars >= MIN_SCRIPT_CHARS and hiragana_ratio > HIRAGANA_DOMINANCE_THRESHOLD:
        return QueryRoute(LANE_KEYWORD, ("hiragana_dominant",), features)

    return QueryRoute(LANE_SEMANTIC, ("default_semantic",), features)
