"""Tool profiles: the endpoint URL picks which tools ``tools/list`` returns.

Issue #1601. ``tools/list`` handed every client all of the registry — about
25,000 tokens of schemas, paid again on every session by a client that loads
them eagerly — although most sessions use a handful of memory tools. The server
cannot control a client's caching, but the client's local MCP configuration
already stores the endpoint URL, so that is where the choice lives::

    /mcp/w/{workspace_id}?profile=core
    /mcp/w/{workspace_id}?tools=remember,recall,reference

``tools`` is an explicit allowlist and wins over ``profile``; with neither (or
``profile=full``) the list is exactly ``get_tool_definitions()``.

A profile is a VIEW, NOT AN AUTHORIZATION BOUNDARY. It filters ``tools/list``
and nothing else: ``tools/call`` never reads it, so a tool left out of the list
stays callable, subject to the same role checks as before. Do not use a profile
to restrict what a key can do — that is what workspace and context roles are
for.
"""

from urllib.parse import parse_qsl

from mcp_server.tools._definitions import get_tool_definitions
from utils.logger import get_logger

logger = get_logger(__name__)

# The memory read/write loop plus what it needs to find its way around:
# contexts, tags, pinned and upcoming memories, feedback. Registry order (the
# order ``tools/list`` answers in), pinned by ``tests/mcp_server/test_tool_profiles.py``
# together with a character budget — adding a tool here costs every ``core`` client.
CORE_TOOLS: tuple[str, ...] = (
    "remember",
    "update_memory",
    "recall",
    "reference",
    "recall_upcoming",
    "load_pinned",
    "forget",
    "explore",
    "get_context_info",
    "list_contexts",
    "list_tags",
    "feedback",
)

# ``None`` = no filter. Insertion order is the order error messages list them in.
PROFILES: dict[str, tuple[str, ...] | None] = {"full": None, "core": CORE_TOOLS}

# Names read from one ``tools`` value; the rest is ignored. The registry holds
# far fewer tools, so a longer list is a mistake or abuse, never a real request.
MAX_TOOL_NAMES = 100

# The query string is client-supplied: what gets logged or echoed back in an
# error message is capped so a crafted URL cannot bloat either.
_SHOWN_NAMES = 20
_SHOWN_CHARS = 64


class ToolProfileError(Exception):
    """The URL's ``profile`` / ``tools`` selection cannot be served.

    The transports answer it with JSON-RPC ``-32602`` (invalid params);
    ``message`` is written for the person fixing their MCP configuration.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _shown(names: list[str]) -> list[str]:
    return [name[:_SHOWN_CHARS] for name in names[:_SHOWN_NAMES]]


def select_tool_definitions(query_string: bytes | str | None) -> list[dict]:
    """Return the tool definitions the endpoint URL asks ``tools/list`` for.

    Reads two query parameters (the first value wins when one repeats; every
    other parameter is left alone):

    - ``tools``: comma-separated tool names. Names are trimmed, case-sensitive
      and de-duplicated; only the first ``MAX_TOOL_NAMES`` are read. Unknown
      names are ignored and logged once.
    - ``profile``: a key of ``PROFILES``. Not read when ``tools`` is present.

    Args:
        query_string: The raw ASGI ``scope["query_string"]`` (percent-encoded
            bytes), the same as ``str``, or ``None``.

    Returns:
        The selected definitions, always in registry order. With no selection
        this is ``get_tool_definitions()`` unchanged.

    Raises:
        ToolProfileError: ``profile`` names no known profile, or ``tools``
            matches no known tool.
    """
    definitions = get_tool_definitions()
    if not query_string:
        return definitions

    if isinstance(query_string, bytes):
        query_string = query_string.decode("utf-8", "replace")
    params: dict[str, str] = {}
    for key, value in parse_qsl(query_string, keep_blank_values=True):
        params.setdefault(key, value)

    if "tools" in params:
        parts = params["tools"].split(",", MAX_TOOL_NAMES)
        truncated = len(parts) > MAX_TOOL_NAMES
        # dict.fromkeys: de-duplicate, keep the order the client wrote.
        requested = list(
            dict.fromkeys(name for part in parts[:MAX_TOOL_NAMES] if (name := part.strip()))
        )
        known = {tool["name"] for tool in definitions}
        unknown = [name for name in requested if name not in known]
        if unknown or truncated:
            logger.info(
                "mcp_tool_profile_names_ignored",
                unknown=_shown(unknown),
                unknown_count=len(unknown),
                truncated=truncated,
            )
        wanted = set(requested) & known
        if not wanted:
            named = f" (unknown: {', '.join(map(repr, _shown(unknown)))})" if unknown else ""
            raise ToolProfileError(
                f"Invalid params: the 'tools' query parameter matches no known tool{named}. "
                "List tools without it to see the valid names."
            )
        return [tool for tool in definitions if tool["name"] in wanted]

    if "profile" in params:
        profile = params["profile"].strip()
        if profile not in PROFILES:
            raise ToolProfileError(
                f"Invalid params: unknown tool profile {profile[:_SHOWN_CHARS]!r}. "
                f"Valid profiles: {', '.join(PROFILES)}."
            )
        selected = PROFILES[profile]
        if selected is not None:
            return [tool for tool in definitions if tool["name"] in selected]

    return definitions
