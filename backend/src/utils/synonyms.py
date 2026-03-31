"""Sudachi synonym dictionary for query-time synonym expansion.

Issue #69: Loads Sudachi synonyms.txt at startup and provides O(1)
synonym lookup for BM25 query expansion.

Format: CSV with group_id as first column, surface form at column index 8.
Empty lines separate groups. All surfaces in the same group are synonyms.

Source: https://github.com/WorksApplications/SudachiDict/blob/develop/src/main/text/synonyms.txt
License: Apache 2.0
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from utils.logger import get_logger

logger = get_logger(__name__)

# Lazy-loaded singleton
_synonym_dict: dict[str, list[str]] | None = None

# Cap expanded tokens to prevent BM25 score distortion from large synonym groups
MAX_EXPANDED_TOKENS = 50

# Default path: backend/src/data/sudachi_synonyms.txt
_DEFAULT_PATH = Path(__file__).parent.parent / "data" / "sudachi_synonyms.txt"


def _load_synonyms(path: str | Path | None = None) -> dict[str, list[str]]:
    """Parse Sudachi synonyms.txt into {surface: [all_synonyms]} dict.

    Args:
        path: Path to synonyms.txt (default: backend/data/sudachi_synonyms.txt)

    Returns:
        Dict mapping each surface form to its synonym group (including itself)
    """
    filepath = Path(path) if path else _DEFAULT_PATH
    if not filepath.exists():
        logger.warning("sudachi_synonyms_not_found", path=str(filepath))
        return {}

    # Group surfaces by group_id
    groups: dict[str, list[str]] = defaultdict(list)

    with open(filepath, encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or len(row) < 9:
                continue
            group_id = row[0].strip()
            surface = row[8].strip()
            if group_id and surface:
                groups[group_id].append(surface)

    # Build reverse lookup: surface → all synonyms in its group
    synonym_dict: dict[str, list[str]] = {}
    for surfaces in groups.values():
        if len(surfaces) < 2:
            continue  # Skip single-entry groups (no synonyms)
        for surface in surfaces:
            # Include all group members except self
            others = [s for s in surfaces if s != surface]
            if surface in synonym_dict:
                # Surface appears in multiple groups — merge
                existing = set(synonym_dict[surface])
                existing.update(others)
                synonym_dict[surface] = list(existing)
            else:
                synonym_dict[surface] = others

    logger.info(
        "sudachi_synonyms_loaded",
        entries=len(synonym_dict),
        groups=len(groups),
        path=str(filepath),
    )
    return synonym_dict


def get_synonym_dict() -> dict[str, list[str]]:
    """Get the lazy-loaded synonym dictionary (singleton).

    Returns:
        Dict mapping surface forms to their synonyms
    """
    global _synonym_dict
    if _synonym_dict is None:
        _synonym_dict = _load_synonyms()
    return _synonym_dict


def expand_synonyms(token: str) -> list[str]:
    """Expand a token to include its synonyms.

    Args:
        token: A single token (Sudachi normalized form)

    Returns:
        List containing the token itself + any synonyms.
        Returns [token] if no synonyms found.
    """
    synonyms = get_synonym_dict().get(token, [])
    if synonyms:
        return [token, *synonyms]
    return [token]


def expand_query_tokens(tokens_str: str) -> str:
    """Expand all tokens in a query with their synonyms.

    Args:
        tokens_str: Space-separated tokens (from tokenize_for_search)

    Returns:
        Space-separated expanded tokens (original + synonyms)
    """
    if not tokens_str:
        return ""

    expanded = []
    for token in tokens_str.split():
        expanded.extend(expand_synonyms(token))

    # Deduplicate while preserving order, cap to prevent BM25 distortion
    seen: set[str] = set()
    result = []
    for t in expanded:
        if t not in seen:
            seen.add(t)
            result.append(t)
            if len(result) >= MAX_EXPANDED_TOKENS:
                logger.debug("synonym_expansion_capped", original_count=len(expanded))
                break

    return " ".join(result)
