"""External client initialization: embedder, Qdrant, Mem0.

Each client degrades to ``None`` on failure (logged), so the server can run in a
reduced mode (e.g. semantic search disabled) instead of crashing at import.
"""
import os

import config
from config import log

# Optional external clients (Gemini embeddings via google-genai, qdrant, mem0).
# Wrap the imports themselves so the pure-logic layer (security, skills, registry)
# and its tests can run in an environment without these packages installed. Each
# client degrades to None and the dependent tools report "offline".
embedder = None
qdrant_db = None
mem0_client = None

# Imported at module scope, and the adapter below defined unconditionally, so its
# batch contract can be tested without an API key. `types` is only touched inside
# embed(), so a missing package costs nothing until something actually embeds.
try:
    from google import genai
    from google.genai import types
except Exception:
    genai = types = None


class GeminiEmbedderAdapter:
    def __init__(self, api_key: str, model_name: str, dimension: int):
        self.client = genai.Client(api_key=api_key)
        self.model_name = model_name
        self.dimension = dimension

    def embed(self, items: list, task_type: str | None = None) -> list[list[float]]:
        """Embed each item as its OWN document: N items in, N vectors out.

        task_type enables asymmetric retrieval: documents are embedded as
        RETRIEVAL_DOCUMENT at index time and the query as RETRIEVAL_QUERY at search
        time, which measurably improves ranking over embedding both symmetrically.
        Callers that don't care pass None (default).
        """
        # Each item goes in wrapped in its own list. The SDK groups CONSECUTIVE plain
        # items into a single Content — a flat list of N chunk strings arrives as one
        # document of N parts and comes back as ONE vector, which reads as a broken API
        # response rather than a malformed request. A list is the one shape it treats as
        # a document boundary; an item that is already one (a multimodal chunk: image
        # parts plus its text) is passed through, since wrapping it again would nest.
        contents = [item if isinstance(item, list) else [item] for item in items]
        cfg = types.EmbedContentConfig(
            output_dimensionality=self.dimension,
            task_type=task_type,
        )
        response = self.client.models.embed_content(
            model=self.model_name, contents=contents, config=cfg)
        vectors = [emb.values for emb in response.embeddings]
        if len(vectors) != len(items):
            # Raise rather than return short: callers that embed a single item subscript
            # [0] directly, so a truncated batch would surface as an IndexError with no
            # indication of what went wrong.
            raise RuntimeError(
                f"embedder returned {len(vectors)} vectors for {len(items)} items "
                f"(model={self.model_name})")
        return vectors


def probe_embedder(candidate):
    """Verify the embedder at startup; return it, or None to disable semantic search.

    Without this a wrong EMBED_MODEL would only fail at the first search, silently
    degrading the feature. TWO items are sent, because one cannot show batching: a
    note is only chunked past a size most are under, so a model answering a batch
    with a single vector would stay invisible until some longer note failed to index.
    """
    try:
        probe = list(candidate.embed(["healthcheck probe", "second probe item"]))
        dim = len(probe[0]) if probe and probe[0] is not None else 0
    except RuntimeError as e:
        # The batch contract, not the network: this model answered a two-item batch with
        # a different number of vectors, so every multi-chunk note would fail to index
        # while single-chunk ones kept working. Not retryable, and an index missing
        # exactly the longer notes is worse than one that is off.
        log.error("Embedding probe: %s. Disabling semantic search — a batch this model "
                  "does not answer one-vector-per-item would index long notes partially "
                  "or not at all.", e)
        return None
    except Exception:
        # A probe EXCEPTION may just be a transient network blip at boot. Unlike a dim
        # mismatch, that's retryable — keep the embedder so the first real call can
        # succeed (or fail visibly) instead of disabling search until a full restart.
        # A bad model id / key will still surface on first use.
        log.warning(
            "Embedding probe failed for model '%s' (transient?). Keeping the embedder; "
            "if calls keep failing, verify GEMINI_API_KEY and that EMBED_MODEL is valid.",
            config.EMBED_MODEL, exc_info=True)
        return candidate
    if dim != config.EMBED_DIM:
        # A confirmed dimension mismatch is a HARD config error (wrong
        # EMBED_MODEL/EMBED_DIM) — disable to avoid a corrupt mixed-dim index.
        log.error(
            "Embedding probe returned dimension %s but EMBED_DIM=%s — check EMBED_MODEL/"
            "EMBED_DIM. Disabling semantic search to avoid a dimension-mismatch index.",
            dim, config.EMBED_DIM)
        return None
    log.info("Embedder ready: model=%s dim=%s", config.EMBED_MODEL, config.EMBED_DIM)
    return candidate


try:
    if config.GEMINI_API_KEY:
        embedder = probe_embedder(
            GeminiEmbedderAdapter(config.GEMINI_API_KEY, config.EMBED_MODEL, config.EMBED_DIM))
    else:
        log.warning("GEMINI_API_KEY not set. Semantic search disabled.")
except Exception:
    log.exception("Embedder unavailable; semantic search disabled.")

def connect_qdrant():
    """(Re)connect to Qdrant and ensure the collection exists. Returns the client
    or None. Extracted so it can run at import AND lazily reconnect later if the
    store was down at startup or died mid-run."""
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams
        db = QdrantClient(host=config.QDRANT_HOST, port=config.QDRANT_PORT)
        try:
            db.get_collection(config.COLLECTION_NAME)
        except Exception:
            db.create_collection(
                collection_name=config.COLLECTION_NAME,
                vectors_config=VectorParams(size=config.EMBED_DIM, distance=Distance.COSINE),
            )
        return db
    except Exception:
        log.exception("Qdrant unavailable; semantic search disabled.")
        return None


qdrant_db = connect_qdrant()


def probe_ready() -> bool:
    """Live readiness probe used by /readyz. Confirms the embedder is configured
    AND Qdrant is actually reachable right now (not just that a handle exists). If
    Qdrant was down at startup, attempt a one-shot lazy reconnect so the server can
    self-heal without a restart."""
    global qdrant_db
    if embedder is None:
        return False
    if qdrant_db is None:
        qdrant_db = connect_qdrant()  # lazy reconnect
        if qdrant_db is None:
            return False
    try:
        qdrant_db.get_collections()  # cheap live ping
        return True
    except Exception:
        log.warning("Qdrant liveness ping failed; marking not-ready and dropping handle.")
        qdrant_db = None  # force a reconnect on the next probe
        return False

def connect_mem0():
    """Wire Mem0 to Gemini and return the client, or None. Extracted so the
    telemetry opt-out and the Gemini pinning are testable without reloading this
    module (which would re-run the embedder probe against the live API)."""
    if not config.GEMINI_API_KEY:
        # Without an explicit provider, Mem0 defaults its LLM + embedder to OpenAI
        # — which both contradicts our "Gemini-only" design and would silently
        # egress personal facts to a third party. Refuse to fall back: keep Mem0
        # off unless we can wire it to Gemini like the rest of the stack.
        log.warning("GEMINI_API_KEY not set — Mem0 implicit memory disabled "
                    "(refusing to fall back to the OpenAI default).")
        return None
    try:
        # mem0ai bundles opt-OUT PostHog telemetry: it fires on Memory() init and
        # sends a stable install UUID plus host/OS/CPU and collection metadata to
        # us.i.posthog.com. No note text, but PRIVACY.md enumerates every egress
        # destination, and this is not one of them — so default it off. setdefault,
        # so an operator who exports MEM0_TELEMETRY=True keeps that choice. Must be
        # set BEFORE the import: mem0.memory.telemetry reads it at module scope.
        os.environ.setdefault("MEM0_TELEMETRY", "False")
        from mem0 import Memory
        # Pin BOTH the LLM and the embedder to Gemini so nothing routes to OpenAI.
        return Memory.from_config({
            "vector_store": {"provider": "qdrant", "config": {
                "host": config.QDRANT_HOST, "port": config.QDRANT_PORT, "embedding_model_dims": config.EMBED_DIM}},
            "llm": {"provider": "gemini", "config": {
                "model": config.LLM_MODEL, "api_key": config.GEMINI_API_KEY}},
            "embedder": {"provider": "gemini", "config": {
                "model": config.EMBED_MODEL, "api_key": config.GEMINI_API_KEY}},
        })
    except Exception:
        log.exception("Mem0 unavailable; implicit memory disabled.")
        return None


mem0_client = connect_mem0()
