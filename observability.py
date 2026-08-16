"""Observability: structured logging, request correlation, and metrics.

Self-contained and dependency-free (stdlib only), so it can be imported before
config finishes setting up and never creates an import cycle (config -> observability,
never the reverse). The in-memory registry below serves the Prometheus text exposition
format itself; prometheus_client is deliberately not a dependency, since the handful of
counters and histograms here do not justify one that must load before config.

Security: this module must never emit secrets. Client identities are hashed by the
caller before they reach a log field or metric label; nothing here logs raw tokens
or note contents.
"""
import os
import sys
import json
import time
import logging
import threading
import contextvars

# --- request correlation ----------------------------------------------------
# Set per-request in the middleware; auto-injected into every log line.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def set_request_id(value: str) -> None:
    request_id_var.set(value)


def get_request_id() -> str:
    return request_id_var.get()


# --- structured logging ------------------------------------------------------
# Reserved LogRecord attributes — anything else passed via `extra=` is treated as
# a structured field and serialized into the JSON line.
_RESERVED = set(vars(logging.makeLogRecord({})))


class JSONFormatter(logging.Formatter):
    """One JSON object per log record, with request_id and any `extra=` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": get_request_id(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging() -> logging.Logger:
    """Install the root handler on stderr. LOG_FORMAT=json|text, LOG_LEVEL=INFO."""
    fmt = os.environ.get("LOG_FORMAT", "json").lower()
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stderr)
    if fmt == "text":
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [%(request_id)s]: %(message)s"))
        # text mode needs request_id present on the record
        handler.addFilter(_RequestIdFilter())
    else:
        handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    return logging.getLogger("cogitobase")


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


# --- metrics registry (dependency-free, Prometheus-compatible) ---------------
class _Counter:
    def __init__(self):
        self._values: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def inc(self, labels: tuple = (), amount: float = 1.0):
        # inc() runs from both the event loop (dispatch) and to_thread workers
        # (git_sync), so the read-modify-write must be atomic or counts are lost.
        with self._lock:
            self._values[labels] = self._values.get(labels, 0.0) + amount

    def samples(self):
        with self._lock:
            return list(self._values.items())


class _Gauge:
    def __init__(self):
        self._values: dict[tuple, float] = {}
        self._lock = threading.Lock()

    def set(self, value: float, labels: tuple = ()):
        with self._lock:
            self._values[labels] = value

    def samples(self):
        with self._lock:
            return list(self._values.items())


# Histogram buckets in seconds — tuned for tool latency (embeddings/git dominate).
_BUCKETS = (0.005, 0.025, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0)


class _Histogram:
    def __init__(self):
        # per label-set: (bucket_counts list, sum, count)
        self._data: dict[tuple, list] = {}
        self._lock = threading.Lock()

    def observe(self, value: float, labels: tuple = ()):
        with self._lock:
            entry = self._data.get(labels)
            if entry is None:
                entry = [[0] * len(_BUCKETS), 0.0, 0]
                self._data[labels] = entry
            counts, total, n = entry
            for i, b in enumerate(_BUCKETS):
                if value <= b:
                    counts[i] += 1
            entry[1] = total + value
            entry[2] = n + 1

    def samples(self):
        with self._lock:
            # Copy bucket lists so a concurrent observe() can't mutate them mid-render.
            return [(labels, [list(counts), total, n])
                    for labels, (counts, total, n) in self._data.items()]


class MetricsRegistry:
    """Minimal, thread-safe, prozesslokal. Label cardinality is the caller's
    responsibility — only finite label sets (tool names, outcomes) are used."""

    def __init__(self):
        self._lock = threading.Lock()
        self._metrics: dict[str, tuple] = {}  # name -> (type, help, label_names, obj)

    def counter(self, name, help_text, label_names=()):
        return self._get_or_create(name, "counter", help_text, label_names, _Counter)

    def gauge(self, name, help_text, label_names=()):
        return self._get_or_create(name, "gauge", help_text, label_names, _Gauge)

    def histogram(self, name, help_text, label_names=()):
        return self._get_or_create(name, "histogram", help_text, label_names, _Histogram)

    def _get_or_create(self, name, mtype, help_text, label_names, factory):
        with self._lock:
            existing = self._metrics.get(name)
            if existing is None:
                obj = factory()
                self._metrics[name] = (mtype, help_text, tuple(label_names), obj)
                return obj
            return existing[3]

    def render(self) -> str:
        """Prometheus text exposition format."""
        lines = []
        with self._lock:
            items = list(self._metrics.items())
        for name, (mtype, help_text, label_names, obj) in items:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {mtype}")
            if mtype == "histogram":
                for labels, (counts, total, n) in obj.samples():
                    cumulative = 0
                    for i, b in enumerate(_BUCKETS):
                        cumulative = counts[i]
                        lbl = _fmt_labels(label_names, labels, extra=("le", _num(b)))
                        lines.append(f"{name}_bucket{lbl} {cumulative}")
                    lbl_inf = _fmt_labels(label_names, labels, extra=("le", "+Inf"))
                    lines.append(f"{name}_bucket{lbl_inf} {n}")
                    base = _fmt_labels(label_names, labels)
                    lines.append(f"{name}_sum{base} {total}")
                    lines.append(f"{name}_count{base} {n}")
            else:
                for labels, value in obj.samples():
                    lines.append(f"{name}{_fmt_labels(label_names, labels)} {_num(value)}")
        return "\n".join(lines) + "\n"


def _num(v) -> str:
    f = float(v)
    return str(int(f)) if f.is_integer() else repr(f)


def _fmt_labels(label_names, label_values, extra=None) -> str:
    pairs = []
    for n, v in zip(label_names, label_values):
        pairs.append(f'{n}="{_escape(str(v))}"')
    if extra:
        pairs.append(f'{extra[0]}="{_escape(str(extra[1]))}"')
    return "{" + ",".join(pairs) + "}" if pairs else ""


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


# Single shared registry instance.
metrics = MetricsRegistry()


class timer:
    """Context manager: observe elapsed seconds into a histogram with given labels."""

    def __init__(self, histogram, labels: tuple = ()):
        self._h = histogram
        self._labels = labels

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self._h.observe(time.perf_counter() - self._start, self._labels)
        return False
