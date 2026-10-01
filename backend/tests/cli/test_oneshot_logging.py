"""The one-shot CLIs configure logging the way the API does (#1788).

``setup_logger`` only ran from ``api/main.py``, so a CLI process left
structlog unconfigured: every level, debug included, rendered to stdout.
``transfer_context_creator --apply`` on a real workspace printed one
``memory_payload_updated_in_qdrant`` line per memory — 1.4 MB for the
operator to scroll past — on top of the plan report. No database needed.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import structlog

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli import apply_rerank_defaults, transfer_context_creator  # noqa: E402
from cli._oneshot import (  # noqa: E402
    INSECURE_QDRANT_WARNING,
    configure_logging,
    silence_insecure_qdrant_warning,
)


@pytest.fixture(autouse=True)
def _plain_logs(monkeypatch):
    # Deterministic renderer regardless of the environment (CI sets
    # LOG_COLORIZE=false; a dev shell may not). The event name is what the
    # assertions look for and both renderers print it verbatim.
    monkeypatch.setenv("LOG_COLORIZE", "false")
    yield
    structlog.reset_defaults()


def test_info_hides_the_per_memory_debug_line_and_keeps_stdout_for_the_report(capsys, monkeypatch):
    # The API container commonly runs with LOG_LEVEL=DEBUG; the CLI's own
    # level must win or the operator gets the 1.4 MB again.
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    configure_logging("INFO")
    log = structlog.get_logger("test.oneshot.info")
    log.debug("memory_payload_updated_in_qdrant", memory_id="m1")
    log.info("qdrant_client_initialized")
    out, err = capsys.readouterr()
    assert "memory_payload_updated_in_qdrant" not in out + err
    assert "qdrant_client_initialized" in err
    assert out == ""


def test_debug_is_still_available_on_request(capsys):
    configure_logging("DEBUG")
    structlog.get_logger("test.oneshot.debug").debug("memory_payload_updated_in_qdrant")
    out, err = capsys.readouterr()
    assert "memory_payload_updated_in_qdrant" in err
    assert out == ""


def test_warning_level_hides_info(capsys):
    configure_logging("WARNING")
    log = structlog.get_logger("test.oneshot.warning")
    log.info("qdrant_client_initialized")
    log.warning("payload_update_failed")
    out, err = capsys.readouterr()
    assert "qdrant_client_initialized" not in err
    assert "payload_update_failed" in err
    assert out == ""


@pytest.mark.parametrize(
    ("url", "api_key", "silenced"),
    [
        ("http://qdrant:6333", "secret", True),
        ("https://qdrant.example", "secret", False),
        ("http://qdrant:6333", "", False),
    ],
)
def test_insecure_qdrant_warning_is_silenced_only_for_plain_http_with_a_key(url, api_key, silenced):
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        silence_insecure_qdrant_warning(url, api_key)
        warnings.warn(INSECURE_QDRANT_WARNING, UserWarning, stacklevel=2)
        # An unrelated UserWarning is never swallowed.
        warnings.warn("something else", UserWarning, stacklevel=2)
    messages = [str(w.message) for w in seen]
    assert (INSECURE_QDRANT_WARNING not in messages) is silenced
    assert "something else" in messages


@pytest.mark.parametrize(
    ("module", "argv"),
    [
        (transfer_context_creator, ["--from", "a", "--to", "b", "--workspace", str(uuid4())]),
        (apply_rerank_defaults, ["--all"]),
    ],
)
def test_parse_accepts_log_level_and_defaults_to_info(module, argv):
    assert module._parse(argv).log_level == "INFO"
    assert module._parse([*argv, "--log-level", "DEBUG"]).log_level == "DEBUG"
    with pytest.raises(SystemExit):
        module._parse([*argv, "--log-level", "TRACE"])


@pytest.mark.parametrize(
    ("module", "argv"),
    [
        (transfer_context_creator, ["--from", "a", "--to", "b", "--workspace", str(uuid4())]),
        (apply_rerank_defaults, ["--all"]),
    ],
)
async def test_main_configures_logging_before_the_scaffold_runs(module, argv):
    order: list[str] = []
    with (
        patch.object(
            module, "configure_logging", side_effect=lambda level: order.append(level)
        ) as configure,
        patch.object(
            module, "run_plan_apply", AsyncMock(side_effect=lambda **_: order.append("run") or 0)
        ),
    ):
        assert await module._main(module._parse([*argv, "--log-level", "WARNING"])) == 0
    configure.assert_called_once_with("WARNING")
    assert order == ["WARNING", "run"]
