"""The one-shot CLIs configure logging the way the API does (#1788).

``setup_logger`` only ran from ``api/main.py``, so a CLI process left
structlog unconfigured: every level, debug included, rendered to stdout.
``transfer_context_creator --apply`` on a real workspace printed one
``memory_payload_updated_in_qdrant`` line per memory — 1.4 MB for the
operator to scroll past — on top of the plan report. No database needed.
"""

from __future__ import annotations

import ast
import logging
import sys
import uuid
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import structlog

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli import (  # noqa: E402
    apply_rerank_defaults,
    restore_context,
    sweep_orphan_vectors,
    transfer_context_creator,
)
from cli._oneshot import INSECURE_QDRANT_WARNING, configure_logging  # noqa: E402

_PER_REQUEST = ("httpx", "httpcore")


@pytest.fixture(autouse=True)
def _no_structlog_reconfigure():
    """Override the suite-wide stub: these tests exercise the real configuration."""


def _fresh_logger():
    # cache_logger_on_first_use caches per proxy — a unique name guarantees
    # the logger picks up the configuration under test.
    return structlog.get_logger(f"oneshot-test-{uuid.uuid4().hex}")


def test_info_hides_the_per_memory_debug_line_and_keeps_stdout_for_the_report(capsys, monkeypatch):
    # The API container commonly runs with LOG_LEVEL=DEBUG; the CLI's own
    # level must win or the operator gets the 1.4 MB again.
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    configure_logging("INFO")
    log = _fresh_logger()
    log.debug("memory_payload_updated_in_qdrant", memory_id="m1")
    log.info("qdrant_client_initialized")
    out, err = capsys.readouterr()
    assert "memory_payload_updated_in_qdrant" not in out + err
    assert "qdrant_client_initialized" in err
    assert out == ""
    # stderr is not a terminal here: a redirected log gets no ANSI escapes.
    assert "\x1b[" not in err


def test_debug_is_still_available_on_request(capsys):
    configure_logging("DEBUG")
    _fresh_logger().debug("memory_payload_updated_in_qdrant")
    out, err = capsys.readouterr()
    assert "memory_payload_updated_in_qdrant" in err
    assert out == ""


def test_warning_level_hides_info(capsys):
    configure_logging("WARNING")
    log = _fresh_logger()
    log.info("qdrant_client_initialized")
    log.warning("payload_update_failed")
    out, err = capsys.readouterr()
    assert "qdrant_client_initialized" not in err
    assert "payload_update_failed" in err
    assert out == ""


@pytest.mark.parametrize("name", _PER_REQUEST)
def test_http_client_per_request_lines_are_held_back_below_debug(name):
    # httpx logs "HTTP Request: PUT .../points/payload" at INFO for every
    # request — one per memory on a payload sweep, the very noise #1788 is
    # about, through stdlib logging instead of structlog.
    # Asserted on the effective level, not isEnabledFor(): a logger left
    # ``disabled`` by some earlier logging config (alembic's fileConfig did
    # this before #1808) would make isEnabledFor() report it too.
    http_logger = logging.getLogger(name)
    configure_logging("INFO")
    assert http_logger.getEffectiveLevel() == logging.WARNING
    # An explicit child level is not re-filtered by the root's: ERROR must
    # not let the client's warnings through.
    configure_logging("ERROR")
    assert http_logger.getEffectiveLevel() == logging.ERROR
    configure_logging("DEBUG")
    assert http_logger.getEffectiveLevel() == logging.DEBUG


def test_insecure_qdrant_warning_is_shown_once_not_dropped():
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        warnings.onceregistry.clear()
        configure_logging("INFO")
        for _ in range(3):
            warnings.warn(INSECURE_QDRANT_WARNING, UserWarning, stacklevel=2)
        for _ in range(2):
            warnings.warn("something else", UserWarning, stacklevel=2)
    messages = [str(w.message) for w in seen]
    # Still a signal (a deployment that lost TLS looks exactly like this),
    # just not a repeated one; unrelated warnings are untouched.
    assert messages.count(INSECURE_QDRANT_WARNING) == 1
    assert messages.count("something else") == 2


_CLI_ARGV = [
    (
        transfer_context_creator,
        ["--from", "a", "--to", "b", "--workspace", "00000000-0000-0000-0000-000000000001"],
    ),
    (apply_rerank_defaults, ["--all"]),
    (sweep_orphan_vectors, []),
    (restore_context, ["00000000-0000-0000-0000-000000000002"]),
]


def _imports_run_plan_apply(path: Path) -> bool:
    """Whether the module at ``path`` imports the plan/apply scaffold."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("cli._oneshot", "_oneshot"):
            if any(alias.name in ("run_plan_apply", "*") for alias in node.names):
                return True
        if isinstance(node, ast.Attribute) and node.attr == "run_plan_apply":
            return True  # ``import cli._oneshot`` + ``_oneshot.run_plan_apply(...)``
    return False


def test_every_one_shot_cli_is_covered_by_the_logging_tests():
    # conftest.py stubs ``setup_logger`` for every other CLI test, so the two
    # parametrized tests below are the only place a ``_main`` that forgot
    # ``configure_logging`` (or a ``_parse`` without ``--log-level``) fails.
    # A new command built on the scaffold has to be listed in ``_CLI_ARGV``.
    cli_dir = _BACKEND_SRC / "cli"
    on_the_scaffold = {
        f"cli.{path.stem}"
        for path in cli_dir.glob("*.py")
        if path.name != "_oneshot.py" and _imports_run_plan_apply(path)
    }
    assert on_the_scaffold, "expected one-shot CLIs under src/cli"
    covered = {module.__name__ for module, _ in _CLI_ARGV}
    assert on_the_scaffold - covered == set(), (
        "one-shot CLI(s) missing from _CLI_ARGV: " + ", ".join(sorted(on_the_scaffold - covered))
    )
    assert covered - on_the_scaffold == set(), (
        "_CLI_ARGV lists module(s) that no longer use run_plan_apply: "
        + ", ".join(sorted(covered - on_the_scaffold))
    )


@pytest.mark.parametrize(("module", "argv"), _CLI_ARGV)
def test_parse_accepts_log_level_and_defaults_to_info(module, argv):
    assert module._parse(argv).log_level == "INFO"
    assert module._parse([*argv, "--log-level", "DEBUG"]).log_level == "DEBUG"
    with pytest.raises(SystemExit):
        module._parse([*argv, "--log-level", "TRACE"])


@pytest.mark.parametrize(("module", "argv"), _CLI_ARGV)
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
