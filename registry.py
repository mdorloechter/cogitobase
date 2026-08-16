"""Tool registry.

Each tool module registers its handlers and MCP schema via ``@register(...)``.
The MCP server iterates this registry to build ``list_tools`` and dispatch
``call_tool``. Adding a tool is a local change: one decorator, no central
dispatch function to edit.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from mcp.types import TextContent, Tool

import observability
from observability import metrics

log = logging.getLogger("cogitobase")

# Tool-level instruments. Defined once; the central dispatch records them so
# instrumentation is not scattered across every handler.
_M_CALLS = metrics.counter(
    "mcp_tool_calls_total", "Tool invocations by tool and outcome.", ("tool", "outcome"))
_M_DURATION = metrics.histogram(
    "mcp_tool_duration_seconds", "Tool execution time in seconds.", ("tool",))

# A tool handler takes the MCP arguments dict and returns MCP content.
ToolHandler = Callable[[dict], Awaitable[list[TextContent]]]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: ToolHandler


# The outcome labels on mcp_tool_calls_total. `ok` and `error` are the dispatcher's own
# (a returned result vs a raised exception); the two below are the handler's to declare,
# because only it knows whether a text reply is an answer or a refusal.
OUTCOME_REJECTED = "rejected"    # the caller asked for something impossible: a rule broken,
                                 # invalid input, a name that does not exist, a name taken.
OUTCOME_UNAVAILABLE = "unavailable"  # the server could not serve it right now — a dependency
                                     # is offline or a fetch failed. Nothing the caller can fix.


class ToolResult(list):
    """MCP content plus the outcome label it should be counted under.

    A `list` subclass so it passes through the MCP layer untouched and no handler
    signature has to change: the content is still the list the protocol expects, and
    the label rides alongside it rather than being inferred from the text.
    """

    def __init__(self, items, outcome: str = "ok"):
        super().__init__(items)
        self.outcome = outcome


_REGISTRY: dict[str, ToolSpec] = {}


def register(name: str, description: str, input_schema: dict) -> Callable[[ToolHandler], ToolHandler]:
    """Decorator: register an async tool handler with its MCP schema."""
    def _decorator(func: ToolHandler) -> ToolHandler:
        if name in _REGISTRY:
            raise ValueError(f"Tool already registered: {name}")
        _REGISTRY[name] = ToolSpec(name, description, input_schema, func)
        return func
    return _decorator


def all_tools() -> list[Tool]:
    """Build the MCP Tool list from the registry."""
    return [
        Tool(name=s.name, description=s.description, inputSchema=s.input_schema)
        for s in _REGISTRY.values()
    ]


async def dispatch(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch a tool call to its handler, with central logging + metrics."""
    spec = _REGISTRY.get(name)
    if spec is None:
        _M_CALLS.inc((name, "unknown"))
        log.warning("Unknown tool requested", extra={"event": "tool_unknown", "tool": name})
        raise ValueError(f"Unknown tool: {name}")
    start = time.perf_counter()
    outcome = "ok"
    try:
        result = await spec.handler(arguments)
        # The handler labels its own refusals (see text()). Reading the reply's PROSE
        # instead would tie a metric to a wording nobody treats as a contract: rephrasing
        # a message would silently move it between outcomes, and every refusal not matching
        # the expected opening — a dependency being offline among them — counted as a
        # success, so a server whose Qdrant was down looked perfectly healthy.
        outcome = getattr(result, "outcome", "ok")
        return result
    except Exception:
        outcome = "error"
        log.exception("Tool raised", extra={"event": "tool_error", "tool": name})
        raise
    finally:
        duration = time.perf_counter() - start
        _M_DURATION.observe(duration, (name,))
        _M_CALLS.inc((name, outcome))
        log.info("Tool dispatched", extra={
            "event": "tool_call", "tool": name, "outcome": outcome,
            "duration_ms": round(duration * 1000, 1)})


def text(message: str, outcome: str = "ok") -> ToolResult:
    """Wrap a string as MCP text content, under the outcome it should be counted as.

    Pass OUTCOME_REJECTED for anything the caller could resend differently, and
    OUTCOME_UNAVAILABLE when a dependency is down or a fetch failed — that one is the
    reason the label exists at all: it is the difference between normal operation and a
    broken deployment, and it is invisible in a reply that reads like any other text.
    """
    return ToolResult([TextContent(type="text", text=message)], outcome)
