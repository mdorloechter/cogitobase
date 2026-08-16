"""End-to-end server smoke test: real Starlette app, real auth middleware,
real lifespan startup (git worker), driven via Starlette's TestClient.

No external MCP client needed. Verifies: the app boots through the modern
lifespan (git worker + Streamable HTTP session manager), the auth middleware
rejects missing/bad bearer tokens on /mcp and lets the correct one through,
plus the ingress body cap and rate limit. A full Streamable HTTP protocol
handshake is exercised separately in tests/_live_streamable_check.py.

Run directly:  python tests/e2e_server.py
"""
import os
import sys
from pathlib import Path

os.environ["AUTH_TOKEN"] = "e2e-secret-token"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import git_sync  # noqa: E402

# Neutralize real git I/O so lifespan startup doesn't try to clone/push anything.
git_sync.init_git_repo = lambda: None

import server  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

PASS, FAIL = 0, 0


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}")


def main():
    # Entering the context manager runs the lifespan startup (git worker + cron).
    with TestClient(server.starlette_app) as client:
        check("app boots through lifespan (git worker started)",
              git_sync.git_queue is not None)

        # /mcp with no Authorization header → 401.
        r = client.post("/mcp")
        check("unauthenticated /mcp is rejected (401)", r.status_code == 401)

        # /mcp with a wrong token → 401.
        r = client.post("/mcp", headers={"Authorization": "Bearer wrong"})
        check("wrong bearer token is rejected (401)", r.status_code == 401)

        # A protected path with the correct token must pass the auth gate. A bare POST
        # without a valid MCP session/body is rejected by the transport (400/406), but
        # NOT with 401 — proving we got past the auth middleware.
        r = client.post("/mcp", headers={"Authorization": "Bearer e2e-secret-token"})
        check("correct bearer token passes the auth gate (not 401)", r.status_code != 401)

        # An unrelated path is not gated at all.
        r = client.get("/nonexistent")
        check("non-protected path is not gated by auth", r.status_code in (404, 405))

        auth = {"Authorization": "Bearer e2e-secret-token"}

        # Body-size cap → 413 (oversized Content-Length, authenticated).
        big = config.MAX_REQUEST_BODY_BYTES + 1
        r = client.post("/mcp", headers={**auth, "Content-Length": str(big)},
                        content=b"x" * 10)  # header says huge; rejected before reading body
        check("oversized request body is rejected (413)", r.status_code == 413)

        # Rate limit → 429 after the window budget is spent.
        server._rate_limiter = server.SlidingWindowRateLimiter(3, 60)
        codes = [client.post("/mcp", headers=auth).status_code for _ in range(5)]
        check("rate limit kicks in (429 after budget)", 429 in codes)
        last = client.post("/mcp", headers=auth)
        check("429 response carries Retry-After header",
              last.status_code != 429 or "retry-after" in {k.lower() for k in last.headers})

    print(f"\n==== SERVER E2E: {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
