"""Shared fixtures and helpers for the cogitobase test suite.

Everything here is used by more than one test module. A helper with a single
consumer stays in that module, so this file does not become a second dumping
ground. The tool modules are imported for their side effect: importing them
registers their handlers in the registry, which every `call()` depends on.
"""
import pytest

import config
import registry
# Importing the tool modules registers their handlers in the registry, which is what
# `call()` dispatches through. Here rather than per test module: conftest is imported
# first, so a module that only calls tools needs no import of its own.
import vault    # noqa: F401
import memory   # noqa: F401
import augment  # noqa: F401
import capture  # noqa: F401


@pytest.fixture(autouse=True)
def setup_teardown(tmp_path, monkeypatch):
    # Point all vaults at temp dirs (config is the single source of truth).
    brain = tmp_path / "brain-vault"
    context = tmp_path / "context-vault"
    skills_dir = tmp_path / "skills"
    for d in (brain, context, skills_dir):
        d.mkdir()
    monkeypatch.setattr(config, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(config, "BRAIN_DIR", brain)
    monkeypatch.setattr(config, "CONTEXT_DIR", context)
    monkeypatch.setattr(config, "SKILLS_DIR", skills_dir)
    # Pin enforcement to a known profile so env can't perturb tests.
    monkeypatch.setattr(config, "VAULT_ENFORCEMENT", "balanced")
    monkeypatch.setattr(config, "VAULT_AUTOLINK", "warn")
    monkeypatch.setattr(config, "VAULT_DEDUP_SOFT", 0.92)
    monkeypatch.setattr(config, "VAULT_DEDUP_HARD", 2.0)
    # rule_strength reads the overrides from the ENVIRONMENT, so pinning the profile
    # above is not enough — clear EVERY rule, or the developer's own shell decides
    # whether a test that expects a rejection gets one.
    for rule in config._RULE_NAMES:
        monkeypatch.delenv(f"VAULT_RULE_{rule.upper()}", raising=False)


def call(name, args=None):
    return registry.dispatch(name, args or {})


def _link_outside(tmp_path, link_path):
    """Point `link_path` at a secret file outside the vault. Skips where the
    platform forbids symlink creation (Windows without developer mode)."""
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("HOSTFILE-SECRET-CONTENT", encoding="utf-8")
    link_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        link_path.symlink_to(secret)
    except OSError:
        pytest.skip("symlink creation not permitted in this environment")
    return secret


class _StubEmbedder:
    """Deterministic, network-free embedder. Hashes tokens into a fixed-dim TF
    vector and L2-normalizes it, so shared vocabulary => higher cosine similarity.
    Not semantic, but it makes keyword-overlap ranking meaningful while exercising
    the REAL plumbing (chunking, Qdrant upsert/query/filter/delete) — no Gemini call.
    """
    def __init__(self, dim):
        self.dim = dim

    def _vec(self, text):
        import re
        v = [0.0] * self.dim
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            v[hash(tok) % self.dim] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / norm for x in v]

    def embed(self, texts):   # no task_type param → exercises vault._embed fallback
        return [self._vec(t) for t in texts]


@pytest.fixture
def real_search_stack(monkeypatch):
    """Swap in a real in-memory Qdrant + deterministic embedder so search_vault
    runs its true index→query→score→payload path without a network or Docker."""
    import clients
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    dim = 64
    monkeypatch.setattr(config, "EMBED_DIM", dim)
    db = QdrantClient(":memory:")
    db.create_collection(
        collection_name=config.COLLECTION_NAME,
        vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
    )
    monkeypatch.setattr(clients, "qdrant_db", db)
    monkeypatch.setattr(clients, "embedder", _StubEmbedder(dim))
    return db


PREAMBLE = "## AI Summary\nContext here."


def _counter_value(rendered: str, line_prefix: str) -> float:
    """Extract a metric's current value from Prometheus text (0.0 if absent)."""
    for line in rendered.splitlines():
        if line.startswith(line_prefix + " "):
            return float(line.rsplit(" ", 1)[1])
    return 0.0
