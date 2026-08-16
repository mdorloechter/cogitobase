"""cogitobase MCP server — transport, auth, MCP wiring and startup.

The actual tool logic lives in dedicated modules (vault, memory, augment, skills).
Importing them registers their handlers in the registry; this file only wires the
registry to the MCP protocol and starts the Streamable HTTP transport (/mcp).
"""
import asyncio
import contextlib
import hashlib
import secrets

import uvicorn

from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from mcp.server import Server
from mcp.types import (
    Prompt,
    PromptMessage,
    GetPromptResult,
    TextContent,
    Tool,
)

import config
import registry
import git_sync
import skills
import observability
from observability import metrics
from ratelimit import SlidingWindowRateLimiter

# HTTP/ingress instruments.
_M_HTTP = metrics.counter("mcp_http_requests_total", "HTTP requests by path and status.", ("path", "status"))
_M_RATE_LIMITED = metrics.counter("mcp_rate_limited_total", "Requests rejected by the rate limiter.")
_M_BODY_REJECTED = metrics.counter("mcp_body_rejected_total", "Requests rejected for oversized body.")

# Importing the tool modules triggers their @register(...) decorators.
import vault   # noqa: F401
import memory  # noqa: F401
import augment  # noqa: F401
import capture  # noqa: F401

from config import log

# --- MCP SERVER LOGIC ---
app = Server("cogitobase", version=config.__version__)


@app.list_prompts()
async def list_prompts() -> list[Prompt]:
    return [Prompt(
        name=skills.PROMPT_NAME,
        description="Agentic loop, AI-first vault rules, and your loaded identity/skills context.",
    )]


@app.get_prompt()
async def get_prompt(name: str, arguments: dict | None = None) -> GetPromptResult:
    if name != skills.PROMPT_NAME:
        raise ValueError(f"Unknown prompt: {name}")
    # Inject the identity/skills context so every session starts knowing who I am.
    full_prompt = await asyncio.to_thread(skills.build_prompt)
    return GetPromptResult(
        description="Agentic meta prompt with identity context",
        messages=[PromptMessage(
            role="user",
            content=TextContent(type="text", text=full_prompt),
        )],
    )


# Identity and skill catalog are exposed via a single tool: get_core_context
# returns identity in one call (also backing the startup `instructions` snapshot);
# skill BODIES stay pull-only via get_skill (exposing them as resources would let
# a resource-iterating client bypass the catalog-push/body-pull rule). See
# ARCHITECTURE.md ADR 14.


@app.list_tools()
async def list_tools() -> list[Tool]:
    return registry.all_tools()


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    return await registry.dispatch(name, arguments)


# --- TRANSPORT (MCP transport: Streamable HTTP) ---
# One /mcp route handles everything: POST for JSON-RPC requests, GET for the SSE
# response stream, DELETE to end a session. The session manager owns the per-session
# lifecycle; we only wire its ASGI handler into Starlette and run() it in the lifespan.
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager  # noqa: E402
from mcp.server.streamable_http import TransportSecuritySettings  # noqa: E402

# DNS-rebinding protection is off unless an allowlist is configured (the reverse
# proxy is the first line of defence — see ADR 8 / README §5).
_transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=bool(config.MCP_ALLOWED_HOSTS or config.MCP_ALLOWED_ORIGINS),
    allowed_hosts=config.MCP_ALLOWED_HOSTS,
    allowed_origins=config.MCP_ALLOWED_ORIGINS,
)

def _new_session_manager() -> StreamableHTTPSessionManager:
    return StreamableHTTPSessionManager(
        app=app,
        json_response=config.MCP_JSON_RESPONSE,
        stateless=config.MCP_STATELESS,
        security_settings=_transport_security,
    )


# The manager's run() may be entered only once per instance, so we (re)create it
# on each lifespan startup rather than binding a single module-level instance.
# That keeps a fresh instance per serving window — correct in production (one
# window) and re-entrant across TestClient contexts in the test suite.
session_manager: StreamableHTTPSessionManager | None = None


async def handle_mcp(scope, receive, send):
    """ASGI entry point for the Streamable HTTP transport (mounted at /mcp)."""
    if session_manager is None:
        raise RuntimeError("MCP session manager not started (lifespan not entered).")
    await session_manager.handle_request(scope, receive, send)


async def background_git_cron():
    while True:
        await asyncio.sleep(900)
        await git_sync.enqueue_sync("cron: auto-sync fallback")
        _rate_limiter.prune()  # forget idle clients so the limiter doesn't leak memory


@contextlib.asynccontextmanager
async def lifespan(_starlette_app):
    # Starlette lifespan context manager, entered once per serving window.
    # Starlette invokes this with itself as the argument — NOT the MCP `Server`
    # instance (the module-level `app` from line 51). The parameter is therefore
    # named differently so it can't shadow that global; `app.instructions` below
    # must set it on the MCP Server, or spec-compliant clients never see it.
    global session_manager
    git_sync.init_git_repo()
    skills.seed_skills()                      # Install repo-shipped default skills
    # Freeze the get_core_context bootstrap trigger into the MCP `instructions`
    # field, which spec-compliant clients (e.g. Claude Code) auto-load on connect.
    # Kept to that single line because clients truncate this field to a per-server
    # budget — with nothing behind the trigger, there is nothing to clip. The full
    # meta prompt, skill catalog, and live identity are all delivered by
    # get_core_context, fresh, so nothing load-bearing depends on this field.
    app.instructions = skills.build_static_instructions()
    worker = git_sync.start_worker()
    cron = asyncio.create_task(background_git_cron())
    # The session manager's run() owns the task group for all MCP sessions; it must
    # wrap the serving window and can only be entered once per instance, so build a
    # fresh one each startup.
    session_manager = _new_session_manager()
    async with session_manager.run():
        try:
            yield
        finally:
            cron.cancel()
            # Drain pending git syncs before exiting so queued vault changes still
            # reach the remote mirror instead of being dropped on shutdown.
            if git_sync.git_queue is not None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(git_sync.git_queue.join(), timeout=30)
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker


_rate_limiter = SlidingWindowRateLimiter(
    config.RATE_LIMIT_REQUESTS, config.RATE_LIMIT_WINDOW_SECONDS)


def _rate_key(auth_header: str) -> str:
    """Key the limiter by the bearer token, hashed so it never reaches a log or metric.

    Only authenticated requests get this far — dispatch() returns the 401 before the
    limiter — so there is always a token to key on, and no peer-IP fallback is needed.
    A fallback would also be the wrong key: behind a reverse proxy the peer is the
    proxy, so every unauthenticated caller would land in a single shared bucket.
    """
    return "t:" + hashlib.sha256(auth_header.encode("utf-8")).hexdigest()[:16]


def _token_matches(auth_header: str) -> bool:
    """Constant-time bearer comparison.

    compare_digest only accepts ASCII strings, so a header carrying any other
    byte cannot be the token (AUTH_TOKEN is ASCII by construction) — report it
    as a plain mismatch instead of letting the TypeError become a 500.
    """
    try:
        return secrets.compare_digest(auth_header, f"Bearer {config.AUTH_TOKEN}")
    except TypeError:
        return False


# Paths that require auth (and ingress protection). /metrics is included so it
# never leaks tool names / usage patterns to an unauthenticated caller.
_PROTECTED_PREFIXES = ("/mcp", "/metrics")


class ProtectionMiddleware(BaseHTTPMiddleware):
    """Request correlation + auth + ingress DoS protection + HTTP metrics."""

    async def dispatch(self, request, call_next):
        # Assign/propagate a request id for log correlation on every request.
        rid = request.headers.get("X-Request-ID") or secrets.token_hex(8)
        observability.set_request_id(rid)

        path = request.url.path
        protected = any(path.startswith(p) for p in _PROTECTED_PREFIXES)

        if protected:
            # 1. Auth (fail-closed, constant-time compare).
            auth_header = request.headers.get("Authorization")
            if not auth_header or not _token_matches(auth_header):
                return self._done(request, Response("Unauthorized.", status_code=401), rid)

            # 2. Body size cap — reject oversized requests.
            if config.MAX_REQUEST_BODY_BYTES:
                cl = request.headers.get("content-length")
                if cl is not None:
                    # Fast path: trust the declared length and reject before reading.
                    try:
                        if int(cl) > config.MAX_REQUEST_BODY_BYTES:
                            return self._reject_body(request, rid, path)
                    except ValueError:
                        return self._done(request, Response("Invalid Content-Length.", status_code=400), rid)
                else:
                    # No Content-Length (e.g. chunked/Transfer-Encoding): the header
                    # check above can't see the size, so count bytes as they stream
                    # in and abort the moment they cross the cap — without buffering
                    # the whole (potentially unbounded) body. The consumed bytes are
                    # cached back onto the request so the downstream app still reads
                    # the full body (BaseHTTPMiddleware replays request._body).
                    body = b""
                    async for chunk in request.stream():
                        body += chunk
                        if len(body) > config.MAX_REQUEST_BODY_BYTES:
                            return self._reject_body(request, rid, path)
                    request._body = body

            # 3. Rate limit per client (sliding window). Deliberately after the auth
            # check, so the key is always a token — see _rate_key and ratelimit.py.
            if config.RATE_LIMIT_ENABLED:
                rate_key = _rate_key(auth_header)
                allowed, retry_after = _rate_limiter.check(rate_key)
                if not allowed:
                    _M_RATE_LIMITED.inc()
                    log.warning("Rate limit exceeded", extra={
                        "event": "rate_limited", "client": rate_key, "path": path})
                    return self._done(request, Response("Too Many Requests.", status_code=429,
                                      headers={"Retry-After": str(retry_after)}), rid)

        response = await call_next(request)
        return self._done(request, response, rid)

    def _reject_body(self, request, rid, path):
        _M_BODY_REJECTED.inc()
        log.warning("Oversized request rejected", extra={"event": "body_rejected", "path": path})
        return self._done(request, Response("Payload too large.", status_code=413), rid)

    @staticmethod
    def _done(request, response, rid):
        # Count every HTTP response by a low-cardinality path label + status.
        _M_HTTP.inc((_path_label(request.url.path), str(response.status_code)))
        response.headers["X-Request-ID"] = rid
        return response


def _path_label(path: str) -> str:
    """Collapse to a fixed set of labels so metric cardinality stays bounded."""
    for p in ("/mcp", "/metrics", "/healthz", "/readyz"):
        if path.startswith(p):
            return p
    return "other"


async def metrics_endpoint(request):
    """Prometheus text exposition. Protected by the middleware above."""
    if not config.METRICS_ENABLED:
        return Response("Metrics disabled.", status_code=404)
    return Response(metrics.render(), media_type="text/plain; version=0.0.4")


async def healthz(request):
    """Liveness: the process is up and serving. Unauthenticated by design so an
    orchestrator can probe it without holding the bearer token."""
    return Response("ok", media_type="text/plain")


async def readyz(request):
    """Readiness: dependencies needed to serve search are actually reachable.

    A LIVE probe (not just a handle check): it pings Qdrant so a store that died
    after startup reports 503 instead of a stale 'ready'. The body is intentionally
    generic ('ready' / 'not ready') so an unauthenticated caller learns liveness
    but not which dependency is down.

    Being outside _PROTECTED_PREFIXES also puts it outside the rate limit, and the
    Qdrant ping runs in the shared thread pool — so a request flood here competes
    with indexing work. Accepted: the shipped compose publishes port 8000 on
    loopback only, so the reverse proxy limits this before it is reachable at all
    (SECURITY.md, known accepted limitations)."""
    import clients
    ready = await asyncio.to_thread(clients.probe_ready)
    return Response("ready" if ready else "not ready",
                    status_code=200 if ready else 503, media_type="text/plain")


# All routes and middleware are registered here in one place, after every
# endpoint function is defined. /mcp is a Mount (a raw ASGI sub-app that owns its
# own POST/GET/DELETE + streaming lifecycle); the ops endpoints are plain
# Request->Response Routes. The ProtectionMiddleware (auth, body cap, rate limit)
# wraps them all.
starlette_app = Starlette(
    routes=[
        Mount("/mcp", app=handle_mcp),
        Route("/metrics", endpoint=metrics_endpoint, methods=["GET"]),
        Route("/healthz", endpoint=healthz, methods=["GET"]),
        Route("/readyz", endpoint=readyz, methods=["GET"]),
    ],
    middleware=[Middleware(ProtectionMiddleware)],
    lifespan=lifespan,
)


if __name__ == "__main__":
    uvicorn.run(starlette_app, host="0.0.0.0", port=8000)
