"""End-to-end test of the real search pipeline: a deterministic, network-free
embedder + real (embedded) Qdrant, exercised through the actual tool handlers.

Run directly (NOT under pytest) so we control client init order:
    python tests/e2e_search.py

Embeddings normally come from the Gemini API (see clients.py / ADR 12), which
needs a network + key. To keep this test hermetic it swaps in a deterministic
StubEmbedder (keyword-overlap cosine) and Qdrant ':memory:', so no server, key,
or Docker is needed. Verifies: write→index→search, delete → no orphan vectors
left behind, brain/context vault isolation, and multi-chunk indexing.
"""
import os
import sys
import asyncio
import tempfile
from pathlib import Path

# Configure BEFORE importing the app modules (config reads env at import).
# The StubEmbedder below ignores EMBED_MODEL; only EMBED_DIM matters for Qdrant.
os.environ.setdefault("EMBED_DIM", "384")
os.environ["AUTH_TOKEN"] = "e2e-search-token-2f6b1c9d"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

# Redirect vaults to a temp dir.
_tmp = Path(tempfile.mkdtemp(prefix="mcp-e2e-"))
config.VAULT_ROOT = _tmp
config.BRAIN_DIR = _tmp / "brain-vault"
config.CONTEXT_DIR = _tmp / "context-vault"
config.SKILLS_DIR = _tmp / "skills"
for d in (config.BRAIN_DIR, config.CONTEXT_DIR, config.SKILLS_DIR):
    d.mkdir(parents=True, exist_ok=True)

import clients  # noqa: E402

# Force a real embedded Qdrant regardless of what clients.py resolved at import.
from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client.models import Distance, VectorParams  # noqa: E402

clients.qdrant_db = QdrantClient(":memory:")
if not clients.qdrant_db.collection_exists(config.COLLECTION_NAME):
    clients.qdrant_db.create_collection(
        collection_name=config.COLLECTION_NAME,
        vectors_config=VectorParams(size=config.EMBED_DIM, distance=Distance.COSINE),
    )


class StubEmbedder:
    """Deterministic, network-free embedder. Hashes tokens into a fixed-dim TF
    vector and L2-normalizes it, so shared vocabulary => higher cosine similarity.
    This is NOT semantic, but it makes ranking by keyword overlap meaningful and
    lets us exercise the REAL plumbing (chunking, Qdrant upsert/query/filter/delete)
    without a network call to the Gemini embedding API."""

    def __init__(self, dim):
        self.dim = dim

    def _vec(self, text):
        import re
        v = [0.0] * self.dim
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            v[hash(tok) % self.dim] += 1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / norm for x in v]

    def embed(self, texts):
        return [self._vec(t) for t in texts]


clients.embedder = StubEmbedder(config.EMBED_DIM)

import registry  # noqa: E402
import vault  # noqa: E402  (registers tools)
import skills  # noqa: E402

PASS, FAIL = 0, 0


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}")


async def main():
    print(f"Embedder: {type(clients.embedder).__name__} (dim={config.EMBED_DIM}) | "
          f"Qdrant: real embedded ({type(clients.qdrant_db).__name__})")
    if clients.embedder is None:
        print("ABORT: embedder did not initialize.")
        return 1

    async def call(name, args):
        return await registry.dispatch(name, args)

    # 1. Write two brain notes with distinct semantic content.
    await call("write_note", {
        "vault": "brain", "filename": "python-tips", "title": "Python Tips",
        "type_meta": "concept", "tags": ["python"],
        "content": "Use list comprehensions and generators for memory-efficient iteration in Python."})
    await call("write_note", {
        "vault": "brain", "filename": "garden", "title": "Garden",
        "type_meta": "concept", "tags": ["home"],
        "content": "Tomatoes need full sun and regular watering to ripen in summer."})
    # A context note that must NOT show up in a brain-scoped search.
    await call("write_note", {
        "vault": "context", "filename": "coding-standards", "title": "Standards",
        "type_meta": "standard", "tags": ["rules"],
        "content": "Always write type hints and prefer pure functions when coding."})

    # 2. Ranking-by-keyword-overlap (what the stub CAN prove): a query that shares
    #    literal tokens with the python note must rank it first. This exercises the
    #    real upsert→query→score→payload path. True *semantic* ranking (synonyms)
    #    would need the real Gemini embeddings, which this hermetic test avoids.
    res = await call("search_vault",
                     {"query": "python list comprehensions and generators", "vault": "brain"})
    txt = res[0].text
    first_hit = txt.split("---")[0]
    print("\n[search brain: 'python list comprehensions and generators']")
    print("   first hit: " + first_hit.strip().splitlines()[0])
    check("keyword-overlap ranking puts the python note first", "python-tips.md" in first_hit)
    check("brain search excludes context-vault note", "coding-standards.md" not in txt)

    # 3. Context-scoped search finds the standards note.
    res = await call("search_vault", {"query": "what coding rules should I follow", "vault": "context"})
    check("context search finds coding-standards", "coding-standards.md" in res[0].text)

    # 4. Delete a note → its vectors are gone (no ghost hits).
    cnt_before = clients.qdrant_db.count(config.COLLECTION_NAME).count
    await call("delete_note", {"filename": "garden"})
    cnt_after = clients.qdrant_db.count(config.COLLECTION_NAME).count
    print(f"\n[deindex] point count {cnt_before} -> {cnt_after}")
    check("delete removes exactly the garden vectors", cnt_after == cnt_before - 1)
    res = await call("search_vault", {"query": "growing tomatoes in the sun", "vault": "brain"})
    check("deleted note no longer appears in search", "garden.md" not in res[0].text)

    # 5. Multi-chunk note: a long doc produces multiple points, all searchable.
    long_body = ("INTRO. " * 50) + " The secret pass phrase is ORANGE_FALCON_42. " + ("OUTRO. " * 300)
    await call("write_note", {
        "vault": "brain", "filename": "longdoc", "title": "Long",
        "type_meta": "research", "tags": [], "content": long_body})
    res = await call("search_vault", {"query": "secret pass phrase orange falcon", "vault": "brain"})
    check("content buried mid-document is found (chunking works)", "longdoc.md" in res[0].text)

    print(f"\n==== E2E: {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
