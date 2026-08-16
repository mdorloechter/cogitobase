"""In-memory sliding-window rate limiter for the MCP ingress.

Dependency-free and process-local — correct for the single-instance, single-tenant
deployment (ADR 8). It is a DoS backstop, not a billing meter: it protects against a
runaway client or a leaked token being hammered, not against a distributed attacker
(that is the reverse proxy's job).

It engages only AFTER a request authenticates: server.py's middleware returns the 401
before it consults the limiter, so unauthenticated traffic never reaches here and
token guessing is not throttled by this class. That ordering is on purpose — keying an
unauthenticated caller means keying it by peer IP, which behind a reverse proxy is the
proxy's own address, so every such caller would share one bucket and the first of them
could lock out the rest. Bounding unauthenticated request rates is the proxy's job,
where the real client address is known (ADR 11).

The clock is injectable so tests don't have to sleep.
"""
import time
from collections import deque


class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: float, clock=time.monotonic):
        self.max_requests = max_requests
        self.window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque] = {}

    def check(self, key: str) -> tuple[bool, int]:
        """Record a hit for ``key``. Returns (allowed, retry_after_seconds).

        retry_after is 0 when allowed, else the whole seconds until the oldest
        in-window hit expires.
        """
        now = self._clock()
        cutoff = now - self.window
        dq = self._hits.get(key)
        if dq is None:
            dq = deque()
            self._hits[key] = dq
        # Drop hits that have aged out of the window.
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= self.max_requests:
            retry_after = max(1, int(dq[0] + self.window - now + 0.999))
            return False, retry_after
        dq.append(now)
        return True, 0

    def prune(self) -> None:
        """Forget keys with no in-window hits, so idle clients don't leak memory."""
        cutoff = self._clock() - self.window
        for key in list(self._hits.keys()):
            dq = self._hits[key]
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if not dq:
                del self._hits[key]
