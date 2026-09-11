"""Pin the boot-phase log markers the CI readiness gate reads (#1500).

``.github/scripts/wait-for-api.sh`` names the phase a slow or crashed API boot
was blocked in by grepping two structlog event names out of the API log. The
bats suite for that script fabricates the markers itself, so nothing else ties
the script to ``api/main.py``: renaming either event would leave CI green while
every future gate failure reports the wrong phase. This test is that tie.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = REPO_ROOT / "backend" / "src" / "api" / "main.py"
GATE_SCRIPT = REPO_ROOT / ".github" / "scripts" / "wait-for-api.sh"

BOOT_PHASE_MARKERS = ("application_starting", "application_started")


def test_main_logs_both_boot_phase_markers() -> None:
    source = MAIN_PY.read_text(encoding="utf-8")
    for marker in BOOT_PHASE_MARKERS:
        assert f'logger.info("{marker}"' in source, marker


def test_readiness_gate_greps_the_same_markers() -> None:
    script = GATE_SCRIPT.read_text(encoding="utf-8")
    for marker in BOOT_PHASE_MARKERS:
        assert f'grep -q "{marker}"' in script, marker
