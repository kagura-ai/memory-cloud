"""Ratchet guard against re-introducing raw ``raise HTTPException`` in routes (#992).

#992 Phase 2 froze the error *shape* via the global ``StarletteHTTPException``
handler (``api/main.py``), but the remaining raw ``raise HTTPException`` sites
still emit the placeholder ``HTTP-<status>`` code instead of a semantic
``NAMESPACE-NNN`` one. This ratchet stops the count from GROWING: a new error
path must raise a canonical ``MemoryCloudException`` subclass
(``utils/exceptions.py``), not a raw ``HTTPException``.

The baseline may only ever **decrease** as sites are converted (a post-1.0
gradual improvement). If you legitimately lower it, update ``BASELINE`` to the
new count. ruff cannot easily ban a specific call inside a path, so this
test-based ratchet is the interim mechanism.
"""

import re
from pathlib import Path

# Count of raw ``raise HTTPException`` in src/api/routes/ at the #992 Phase 2
# freeze. RATCHET: may go DOWN (conversions), must never go UP.
BASELINE = 296

_ROUTES = Path(__file__).resolve().parents[2] / "src" / "api" / "routes"
_PATTERN = re.compile(r"raise\s+HTTPException")


def test_raw_httpexception_count_does_not_grow():
    per_file: dict[str, int] = {}
    total = 0
    for path in sorted(_ROUTES.rglob("*.py")):
        n = len(_PATTERN.findall(path.read_text(encoding="utf-8")))
        if n:
            per_file[path.name] = n
            total += n
    assert total <= BASELINE, (
        f"raw `raise HTTPException` count rose to {total} (baseline {BASELINE}). "
        "New error paths must raise a canonical MemoryCloudException subclass "
        f"(utils/exceptions.py), not a raw HTTPException. Per-file: {per_file}"
    )
