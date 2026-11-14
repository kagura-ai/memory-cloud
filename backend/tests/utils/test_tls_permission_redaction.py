"""The name a proxy asks about never reaches a log line (#2809, R10).

The permission call carries a customer host name in its query
(``/internal/public-mcp-domains/tls-permission?domain=<name>``). uvicorn's
access line prints the path with its query, and any structlog field may carry
a URL. The shared scrubber ``_scrub_text`` (the chokepoint of every log path:
the structlog processor, the stdlib formatter and uvicorn's own handlers)
replaces that query with ``domain=[REDACTED]``.
"""

from __future__ import annotations

import io
import logging
import uuid

import pytest
import structlog

from tests.structlog_state import preserved_structlog_config
from utils.logger import _scrub_text, redact_pg_detail, setup_logger

NAME = "alias.example.org"
PATH = "/internal/public-mcp-domains/tls-permission"
REDACTED = f"{PATH}?domain=[REDACTED]"


class TestScrubText:
    def test_the_query_is_replaced(self) -> None:
        assert _scrub_text(f"{PATH}?domain={NAME}") == REDACTED

    def test_access_log_line(self) -> None:
        line = f'198.51.100.7:40312 - "GET {PATH}?domain={NAME} HTTP/1.1" 404'

        out = _scrub_text(line)

        assert NAME not in out
        assert out == f'198.51.100.7:40312 - "GET {REDACTED} HTTP/1.1" 404'

    @pytest.mark.parametrize(
        "query",
        [
            f"domain={NAME}",
            f"domain={NAME}&domain=other.example.org",
            f"x=1&domain={NAME}",
            "domain=Alias.Example.ORG.",
            "domain=alias%2Eexample%2Eorg",
            f"name={NAME}",
            "",
        ],
    )
    def test_the_whole_query_goes_whatever_it_holds(self, query: str) -> None:
        out = _scrub_text(f"GET {PATH}?{query} HTTP/1.1")

        assert out == f"GET {REDACTED} HTTP/1.1"

    def test_a_trailing_slash_and_a_full_url(self) -> None:
        out = _scrub_text(f"http://api-blue:8080{PATH}/?domain={NAME}#x")

        assert NAME not in out
        assert out == f"http://api-blue:8080{PATH}/?domain=[REDACTED]#x"

    def test_quoted_and_nested_in_text(self) -> None:
        out = _scrub_text(f"url='{PATH}?domain={NAME}' next=\"{PATH}?domain={NAME}\"")

        assert NAME not in out
        assert out.count("domain=[REDACTED]") == 2

    @pytest.mark.parametrize(
        "text",
        [
            PATH,
            f"GET {PATH} HTTP/1.1",
            f"/internal/other?domain={NAME}",
            f"/api/v1/resources/x/public-mcp/domains?domain={NAME}",
            f"https://{NAME}/mcp",
        ],
    )
    def test_everything_else_is_untouched(self, text: str) -> None:
        assert _scrub_text(text) == text


class TestStructlogFields:
    def test_fields_and_nested_values_lose_the_query(self) -> None:
        event = {
            "event": f"GET {PATH}?domain={NAME}",
            "url": f"http://api-blue:8080{PATH}?domain={NAME}",
            "request": {"target": f"{PATH}?domain={NAME}", "n": 1},
            "history": [f"{PATH}?domain={NAME}"],
        }

        out = redact_pg_detail(None, "info", event)

        assert NAME not in repr(out)
        assert out["request"]["n"] == 1
        assert out["url"] == f"http://api-blue:8080{REDACTED}"


@pytest.fixture
def _logging_reset():
    root = logging.getLogger()
    access = logging.getLogger("uvicorn.access")
    saved = (root.handlers[:], access.handlers[:], access.propagate, access.level)
    with preserved_structlog_config():
        yield root, access
    root.handlers[:], access.handlers[:], access.propagate, _level = saved
    access.setLevel(_level)


class TestRenderedPipelines:
    def test_uvicorn_access_line(self, monkeypatch, _logging_reset) -> None:
        monkeypatch.setenv("LOG_COLORIZE", "false")
        _root, access = _logging_reset
        sink = io.StringIO()
        handler = logging.StreamHandler(sink)
        handler.setFormatter(logging.Formatter("ACCESS %(message)s"))
        access.handlers[:] = [handler]
        access.propagate = False
        access.setLevel(logging.INFO)

        setup_logger(enable_colors=False)

        # uvicorn's own format and arguments (client, method, path with query, version, status).
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "198.51.100.7:40312",
            "GET",
            f"{PATH}?domain={NAME}",
            "1.1",
            200,
        )
        out = sink.getvalue()
        assert out.startswith("ACCESS ")
        assert NAME not in out
        assert REDACTED in out

    @pytest.mark.parametrize("colors", [False, True])
    def test_structlog_event(self, monkeypatch, capsys, _logging_reset, colors: bool) -> None:
        monkeypatch.setenv("LOG_COLORIZE", "true" if colors else "false")
        setup_logger(enable_colors=colors)

        structlog.get_logger(f"tls-permission-{uuid.uuid4().hex}").warning(
            "request_seen", url=f"http://api-blue:8080{PATH}?domain={NAME}"
        )

        out = capsys.readouterr().out
        assert "request_seen" in out
        assert NAME not in out

    def test_plain_stdlib_logger(self, monkeypatch, capsys, _logging_reset) -> None:
        monkeypatch.setenv("LOG_COLORIZE", "false")
        root, _access = _logging_reset
        root.handlers.clear()
        setup_logger(enable_colors=False)

        logging.getLogger(f"stdlib-tls-{uuid.uuid4().hex}").warning(
            "asked: %s?domain=%s", PATH, NAME
        )

        assert NAME not in capsys.readouterr().out
