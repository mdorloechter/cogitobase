"""The vector index: chunking, upsert, and convergence with the file on disk.

Search runs against a real in-memory Qdrant with a deterministic embedder, so the
index -> query -> score -> payload path is exercised without a network or Docker.
"""
import time
import pytest
import config
import observability
import vault
from security import chunk_text, point_id_for, vault_relative_path
from conftest import _StubEmbedder, _counter_value, call


# ----------------- search_vault (real Qdrant, deterministic embedder) -----------------
@pytest.mark.asyncio
async def test_search_vault_ranks_indexes_isolates_and_deindexes(real_search_stack):
    """The core retrieval path end-to-end against a real embedded Qdrant:
    keyword-overlap ranking, brain/context vault isolation, and the deindex-on-delete
    fix (no ghost vectors). This is the one path only the e2e script covered before."""
    db = real_search_stack

    await call("write_note", {
        "vault": "brain", "filename": "python-tips", "title": "Python Tips",
        "type_meta": "concept", "tags": ["python"],
        "content": "Use list comprehensions and generators for memory-efficient iteration in Python."})
    await call("write_note", {
        "vault": "brain", "filename": "garden", "title": "Garden",
        "type_meta": "concept", "tags": ["home"],
        "content": "Tomatoes need full sun and regular watering to ripen in summer."})
    # A context-vault note that must NOT surface in a brain-scoped search.
    await call("write_note", {
        "vault": "context", "filename": "coding-standards", "title": "Standards",
        "type_meta": "standard", "tags": ["rules"],
        "content": "Always write type hints and prefer pure functions when coding."})

    # Ranking: the query shares literal tokens with the python note → it ranks first.
    res = await call("search_vault",
                     {"query": "python list comprehensions and generators", "vault": "brain"})
    txt = res[0].text
    first_hit = txt.split("---")[0]
    assert "python-tips.md" in first_hit                 # keyword-overlap ranking
    assert "coding-standards.md" not in txt              # brain search excludes context

    # Context-scoped search finds the standards note.
    res = await call("search_vault",
                     {"query": "what coding rules should I follow", "vault": "context"})
    assert "coding-standards.md" in res[0].text

    # Deindex-on-delete: deleting a note removes exactly its vectors (no ghost hits).
    before = db.count(config.COLLECTION_NAME).count
    await call("delete_note", {"filename": "garden"})
    after = db.count(config.COLLECTION_NAME).count
    assert after == before - 1
    res = await call("search_vault", {"query": "growing tomatoes in the sun", "vault": "brain"})
    assert "garden.md" not in res[0].text


@pytest.mark.asyncio
async def test_search_hits_are_named_so_read_note_resolves_them(real_search_stack):
    """A hit must name itself the way read_note takes it back unambiguously —
    otherwise search→read runs into the ambiguity refusal for same-named notes."""
    for type_meta, body in (("concept", "what a roadmap is for in general"),
                            ("project", "roadmap of Q3 milestones and staffing")):
        await call("write_note", {
            "vault": "brain", "filename": "roadmap", "title": "Roadmap",
            "type_meta": type_meta, "tags": [], "content": body, "overwrite": True})

    res = await call("search_vault", {"query": "roadmap of Q3 milestones", "vault": "brain"})
    first_hit = res[0].text.split("---")[0]
    assert "brain-vault/projects/roadmap.md" in first_hit

    # The name the search printed resolves on its own.
    name = first_hit.split("File: ")[1].splitlines()[0].strip()
    res = await call("read_note", {"filename": name})
    assert "Q3 milestones" in res[0].text
    assert "ambiguous" not in res[0].text.lower()


@pytest.mark.asyncio
async def test_a_hit_without_a_path_falls_back_to_its_basename(real_search_stack):
    """Media points carry no `path` — there is no note to resolve — and neither do
    points indexed before the field existed. The basename is all there is to print,
    so printing it must not raise."""
    from qdrant_client.models import PointStruct
    import clients
    real_search_stack.upsert(collection_name=config.COLLECTION_NAME, points=[PointStruct(
        id=1, vector=clients.embedder.embed(["architecture diagram"])[0],
        payload={"filename": "diagram.png", "vault": "media", "chunk_idx": 0,
                 "text": "Image: architecture diagram"})])
    res = await call("search_vault", {"query": "architecture diagram", "vault": "media"})
    assert "File: diagram.png" in res[0].text


@pytest.mark.asyncio
async def test_search_prints_a_score_that_separates_a_find_from_padding(real_search_stack):
    """Qdrant returns the top k whatever the query, so a search with nothing behind it
    still yields hits. Without the score they read like the good ones, and the client
    is required not to claim absence — it needs the number that tells them apart."""
    await call("write_note", {
        "vault": "brain", "filename": "kubernetes-drain", "title": "Drain",
        "type_meta": "concept", "tags": [],
        "content": "## AI Summary\n\nDraining a kubernetes node evicts its pods first."})
    await call("write_note", {
        "vault": "brain", "filename": "sourdough", "title": "Sourdough",
        "type_meta": "concept", "tags": [],
        "content": "## AI Summary\n\nSourdough starter needs flour, water and patience."})

    res = await call("search_vault", {"query": "draining a kubernetes node", "vault": "brain"})
    hits = res[0].text.split("---")
    assert all("Score: " in h for h in hits)

    def score_of(hit):
        return float(hit.split("Score: ")[1].splitlines()[0])

    # Comparative, not a threshold: the stub embedder's absolute numbers are its own
    # business, but the relevant note must outscore the unrelated one.
    by_name = {("drain" if "kubernetes-drain" in h else "bread"): score_of(h) for h in hits}
    assert by_name["drain"] > by_name["bread"]


def test_a_hit_without_a_score_still_renders():
    """Not every point comes from a scoring query — a hand-built or scrolled point has
    no score. The line is left out rather than printed empty, and the hit still names
    its file, because a rendering error would cost the whole result."""
    import types
    hit = vault._render_hit(types.SimpleNamespace(
        payload={"path": "brain-vault/concepts/drain.md", "text": "body"}))
    assert "File: brain-vault/concepts/drain.md" in hit
    assert "Snippet: body" in hit
    assert "Score" not in hit


@pytest.mark.asyncio
async def test_search_honours_and_clamps_the_requested_limit(real_search_stack):
    """`limit` is a caller's request, not a promise: the schema bound is advertised,
    not enforced by the transport, so an out-of-range or non-numeric value must land
    inside 1..20 instead of reaching Qdrant."""
    for i in range(7):
        await call("write_note", {
            "vault": "brain", "filename": f"node-{i}", "title": f"Node {i}",
            "type_meta": "concept", "tags": [],
            "content": f"## AI Summary\n\nDraining kubernetes node number {i} evicts pods."})

    async def hit_count(limit):
        res = await call("search_vault", {"query": "draining kubernetes node",
                                         "vault": "brain", **limit})
        return len(res[0].text.split("---"))

    assert await hit_count({"limit": 1}) == 1
    assert await hit_count({"limit": 7}) == 7
    assert await hit_count({}) == 5                       # the default
    assert await hit_count({"limit": 0}) == 1             # clamped up to the floor
    assert await hit_count({"limit": 999}) == 7           # clamped to 20, capped by the vault
    for junk in ("abc", None, 2.9):
        assert await hit_count({"limit": junk}) >= 1      # no crash, a usable limit


@pytest.mark.asyncio
async def test_search_vault_offline_is_reported(monkeypatch):
    """With no embedder/Qdrant wired, search degrades to a clear message, not a crash."""
    import clients
    monkeypatch.setattr(clients, "embedder", None)
    monkeypatch.setattr(clients, "qdrant_db", None)
    res = await call("search_vault", {"query": "anything", "vault": "brain"})
    assert "offline" in res[0].text.lower()


@pytest.mark.asyncio
async def test_deindex_keeps_the_vectors_of_a_same_named_note(real_search_stack):
    """Two notes can share a basename — a concept and a project both called
    roadmap.md. Deleting one must remove only its own vectors; a basename-scoped
    delete would silently strip the other note out of search while its file stays
    on disk, which no message or metric would reveal."""
    db = real_search_stack
    for type_meta, body in (("concept", "Roadmap as a concept: alpha planning notes."),
                            ("project", "Roadmap for the project: beta delivery dates.")):
        await call("write_note", {
            "vault": "brain", "filename": "roadmap", "title": "Roadmap",
            "type_meta": type_meta, "tags": [], "content": body})
    concept = config.BRAIN_DIR / "concepts" / "roadmap.md"
    project = config.BRAIN_DIR / "projects" / "roadmap.md"
    assert concept.exists() and project.exists()

    vault.deindex_markdown_file(concept)

    points, _ = db.scroll(config.COLLECTION_NAME, limit=100, with_payload=True)
    paths = {p.payload.get("path") for p in points}
    assert vault_relative_path(project) in paths     # the other note is untouched
    assert vault_relative_path(concept) not in paths  # the target is gone
    # And it is still findable, which is what the user would actually notice.
    res = await call("search_vault", {"query": "roadmap beta delivery dates", "vault": "brain"})
    assert "roadmap.md" in res[0].text


@pytest.mark.asyncio
async def test_deindex_removes_a_point_that_carries_no_path(real_search_stack):
    """A point without `path` must still be deletable by (filename, vault), or it
    outlives every delete as a ghost hit that only a full reindex clears."""
    db = real_search_stack
    from qdrant_client.models import PointStruct

    note = config.BRAIN_DIR / "concepts" / "pathless.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Pathless\nsome content", encoding="utf-8")
    db.upsert(config.COLLECTION_NAME, points=[PointStruct(
        id=point_id_for(note, 0),
        vector=[0.0] * config.EMBED_DIM,
        payload={"filename": "pathless.md", "vault": "brain", "chunk_idx": 0,
                 "text": "some content"})])   # no "path" key

    before = db.count(config.COLLECTION_NAME).count
    vault.deindex_markdown_file(note)
    assert db.count(config.COLLECTION_NAME).count == before - 1


# ----------------- index_markdown_file: one vector per chunk -----------------
class _CountingQdrant:
    """Records what was upserted so a test can count points instead of vectors."""
    def __init__(self):
        self.points = []
        self.deletes = []

    def upsert(self, collection_name, points):
        self.points.extend(points)

    def delete(self, collection_name, points_selector):
        self.deletes.append(points_selector)


def _multi_chunk_note(tmp_path):
    """A note whose body is long enough that chunk_text splits it. Derived from
    config.CHUNK_SIZE_CHARS so raising the chunk size can't silently reduce this
    to a single chunk and make the tests vacuous."""
    note = tmp_path / "long.md"
    note.write_text("Alpha beta gamma. " * (config.CHUNK_SIZE_CHARS // 4), encoding="utf-8")
    chunks = chunk_text(note.read_text(encoding="utf-8"))
    assert len(chunks) > 1, "fixture must produce several chunks"
    return note, chunks


def test_index_markdown_refuses_a_partial_embedding_batch(monkeypatch, tmp_path):
    """Zipping fewer vectors than chunks would index only the leading chunks: the
    note's tail stays unfindable while the write reports success. Nothing may be
    indexed, and the failure must be visible in /metrics."""
    import clients
    note, chunks = _multi_chunk_note(tmp_path)

    class TruncatingEmbedder:
        def embed(self, items, task_type=None):
            return [[0.1] * 8]                 # one vector, however many chunks

    db = _CountingQdrant()
    monkeypatch.setattr(clients, "embedder", TruncatingEmbedder())
    monkeypatch.setattr(clients, "qdrant_db", db)

    label = 'mcp_index_failures_total{kind="markdown"}'
    before = _counter_value(observability.metrics.render(), label)
    assert vault.index_markdown_file(note) is False
    assert db.points == []                     # no partial index, not even the first chunk
    assert _counter_value(observability.metrics.render(), label) == before + 1


def test_index_markdown_indexes_every_chunk_when_counts_match(monkeypatch, tmp_path):
    """The positive case, so the length check above cannot silently reject healthy
    batches: one vector per chunk means one point per chunk."""
    import clients
    note, chunks = _multi_chunk_note(tmp_path)

    class FullEmbedder:
        def embed(self, items, task_type=None):
            return [[0.1] * 8 for _ in items]

    db = _CountingQdrant()
    monkeypatch.setattr(clients, "embedder", FullEmbedder())
    monkeypatch.setattr(clients, "qdrant_db", db)

    assert vault.index_markdown_file(note) is True
    assert len(db.points) == len(chunks)


# ----------------- the embedder's batch contract, checked against the real SDK -----------------
def _contents_the_adapter_would_send(items):
    """Run `items` through the shape the Gemini adapter builds, then through the SDK's
    own normaliser, and report how many documents came out.

    The real google-genai is used, not a stub. A stub embedder returns one vector per
    item because that is what the caller means, so it agrees with the caller no matter
    which shape is sent — every mock in this file does, which is why they cannot decide
    this. The SDK's grouping is the thing under test, so only the SDK can answer it.

    `_transformers` is SDK-internal: if it moves, this test breaks and gets pointed at
    the new entry point. That is the intended failure — a stub written to our own
    assumption would keep passing while the contract broke underneath it.
    """
    from google.genai import _transformers
    contents = [item if isinstance(item, list) else [item] for item in items]
    return [len(c.parts) for c in _transformers.t_contents(contents)]


def test_a_batch_of_chunks_is_one_document_each():
    """N items must reach the API as N documents, or the batch comes back as one vector.

    The SDK groups CONSECUTIVE plain items into a single Content, so a flat list of
    chunk strings is one document of N parts — one vector for the whole note. Every
    note long enough to chunk then fails the length check in index_markdown_file and
    is not indexed at all, while short notes keep working and the write reports success.
    """
    pytest.importorskip("google.genai")
    for count in (1, 2, 3, 7):
        parts = _contents_the_adapter_would_send([f"chunk {i}" for i in range(count)])
        assert parts == [1] * count, (
            f"{count} chunks were folded into {len(parts)} document(s) with parts {parts}")


def test_a_multimodal_chunk_stays_one_document_with_its_parts():
    """A chunk that carries images is already a list: its parts belong together.

    So the wrapping must not nest it into a document of one part, and must not let it
    merge with the plain chunks around it either — each item is its own document.
    """
    pytest.importorskip("google.genai")
    from google.genai import types
    image = types.Part.from_bytes(data=b"\x89PNG\r\n\x1a\n", mime_type="image/png")

    assert _contents_the_adapter_would_send([[image, "chunk with an image"]]) == [2]
    # Mixed batch: the multimodal item keeps both parts, the plain ones stay separate.
    assert _contents_the_adapter_would_send(
        [[image, "chunk one"], "chunk two", "chunk three"]) == [2, 1, 1]


def test_the_embedder_refuses_a_short_vector_batch(monkeypatch):
    """A truncated batch must raise, not return short.

    index_pdf_file and index_image_file subscript [0] on the result, so a response with
    no vectors would surface as an IndexError naming neither the model nor the batch.
    """
    pytest.importorskip("google.genai")
    import clients
    adapter = clients.GeminiEmbedderAdapter.__new__(clients.GeminiEmbedderAdapter)
    adapter.model_name = "test-model"
    adapter.dimension = 8

    class _Response:
        embeddings = [type("E", (), {"values": [0.1] * 8})()]

    class _Models:
        def embed_content(self, **kwargs):
            return _Response()                 # one vector, however many items

    adapter.client = type("C", (), {"models": _Models()})()
    with pytest.raises(RuntimeError, match="1 vectors for 3 items"):
        adapter.embed(["a", "b", "c"])
    # The single-item case is the one that always worked, and must keep working.
    assert adapter.embed(["a"]) == [[0.1] * 8]


def test_the_startup_probe_sends_a_batch_and_disables_on_a_folded_one(monkeypatch):
    """A one-item probe cannot see a folded batch, and that is where it hides.

    Most notes fit in one chunk, so an embedder that answers any batch with a single
    vector serves them correctly and fails only on longer ones — at write time, in the
    log, after the tool has already reported success. Probing with two items moves that
    discovery to startup, where the operator is looking."""
    import clients
    seen = {}

    class FoldingEmbedder:
        def embed(self, items, task_type=None):
            seen["count"] = len(items)
            raise RuntimeError(f"embedder returned 1 vectors for {len(items)} items")

    assert clients.probe_embedder(FoldingEmbedder()) is None, \
        "an embedder that folds a batch must not stay installed"
    assert seen["count"] > 1, "a single-item probe cannot reveal a folded batch"


def test_the_startup_probe_keeps_a_healthy_embedder(monkeypatch):
    """The positive case, so the checks above cannot quietly disable a working one."""
    import clients
    monkeypatch.setattr(config, "EMBED_DIM", 8)

    class HealthyEmbedder:
        def embed(self, items, task_type=None):
            return [[0.1] * 8 for _ in items]

    healthy = HealthyEmbedder()
    assert clients.probe_embedder(healthy) is healthy


def test_the_startup_probe_keeps_an_embedder_that_only_blipped(monkeypatch):
    """A network error at boot is retryable, and disabling search until a restart would
    turn a blip into an outage. A wrong key or model id still surfaces on first use."""
    import clients

    class BlippingEmbedder:
        def embed(self, items, task_type=None):
            raise ConnectionError("name resolution failed")

    blipping = BlippingEmbedder()
    assert clients.probe_embedder(blipping) is blipping


# ----------------- index convergence: a shrinking note loses its tail -----------------
def _indexed_chunk_count(path):
    """How many chunks the note on disk currently has — the number of points it
    should occupy. Derived from the note, so a chunk-size change can't make the
    assertions vacuous."""
    return len(chunk_text(vault._body_without_frontmatter(path.read_text(encoding="utf-8"))))


_TAIL_MARKER = "sphygmomanometer"


# Long enough that chunk_text splits it several ways, with the marker confined to the
# tail — and repeated there, so it lands inside the 300-char snippet search prints and
# a surviving tail point is therefore actually observable through search_vault.
_LONG_BODY = ("## AI Summary\n\nNotes on draining nodes.\n\n"
              + "Alpha beta gamma delta. " * (config.CHUNK_SIZE_CHARS // 24)
              + f"The tail of this note repeats {_TAIL_MARKER}. " * (config.CHUNK_SIZE_CHARS // 24))


@pytest.mark.asyncio
async def test_overwriting_with_a_shorter_note_drops_its_tail_chunks(real_search_stack):
    """Point ids are derived from (path, chunk_idx), so re-indexing replaces chunks
    in place — a note that SHRINKS would leave its surplus tail behind, and those
    points keep serving prose the note no longer contains."""
    db = real_search_stack
    note = config.BRAIN_DIR / "concepts" / "drain.md"

    await call("write_note", {"vault": "brain", "filename": "drain", "title": "Drain",
                              "type_meta": "concept", "tags": [], "content": _LONG_BODY})
    assert _indexed_chunk_count(note) > 1, "fixture must produce several chunks"
    assert db.count(config.COLLECTION_NAME).count == _indexed_chunk_count(note)

    await call("write_note", {"vault": "brain", "filename": "drain", "title": "Drain",
                              "type_meta": "concept", "tags": [],
                              "content": "## AI Summary\n\nDraining, in one line.",
                              "overwrite": True})

    assert db.count(config.COLLECTION_NAME).count == _indexed_chunk_count(note) == 1
    res = await call("search_vault", {"query": _TAIL_MARKER, "vault": "brain"})
    assert _TAIL_MARKER not in res[0].text
    # The note itself is still searchable — the drop took the tail, not the note.
    res = await call("search_vault", {"query": "draining in one line", "vault": "brain"})
    assert "drain.md" in res[0].text


@pytest.mark.asyncio
async def test_appending_past_a_chunk_boundary_keeps_every_chunk(real_search_stack):
    """The counterpart: the tail drop starts at the NEW chunk count, so a note that
    GROWS across a chunk boundary must keep all of its points. Dropping from the old
    count would delete the chunks the append just added."""
    db = real_search_stack
    note = config.BRAIN_DIR / "concepts" / "drain.md"

    await call("write_note", {"vault": "brain", "filename": "drain", "title": "Drain",
                              "type_meta": "concept", "tags": [],
                              "content": "## AI Summary\n\nDraining, in one line."})
    assert db.count(config.COLLECTION_NAME).count == 1

    await call("append_to_note", {"filename": "drain", "content": _LONG_BODY})

    chunks = _indexed_chunk_count(note)
    assert chunks > 1
    assert db.count(config.COLLECTION_NAME).count == chunks
    res = await call("search_vault", {"query": _TAIL_MARKER, "vault": "brain"})
    assert _TAIL_MARKER in res[0].text


def test_indexing_skips_a_note_that_vanishes_before_the_upsert(monkeypatch, tmp_path):
    """A rebuild spends minutes in embedding latency; the note may be deleted (or
    replaced by a link) meanwhile. Upserting anyway would republish the plaintext of
    a note that no longer exists, and only a further reindex would clear it."""
    import clients
    note = config.BRAIN_DIR / "concepts" / "doomed.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("secret prose that must not outlive the file", encoding="utf-8")

    class DeletingEmbedder:
        """Stands in for the latency window: the file goes away mid-embed."""
        def embed(self, items, task_type=None):
            note.unlink()
            return [[0.1] * 8 for _ in items]

    db = _CountingQdrant()
    monkeypatch.setattr(clients, "embedder", DeletingEmbedder())
    monkeypatch.setattr(clients, "qdrant_db", db)

    assert vault.index_markdown_file(note) is True   # not a failure, just nothing to do
    assert db.points == []


# ----------------- rename: the file moves first, the index follows -----------------
@pytest.mark.asyncio
async def test_a_failed_rename_leaves_the_note_searchable(real_search_stack, monkeypatch):
    """The rename runs before the index moves, so a rename that fails on the
    filesystem (locked file, lost permission) leaves the note exactly as it was.
    Deindexing up front would strip a note that is still on disk of every vector —
    silently unsearchable, with nothing but a reindex to restore it."""
    await call("write_note", {"vault": "brain", "filename": "alpha", "title": "Alpha",
                              "type_meta": "concept", "tags": [],
                              "content": "## AI Summary\n\nAlpha explains node draining."})

    def refuse(self, target):
        raise OSError("rename refused by the filesystem")

    monkeypatch.setattr(vault.Path, "rename", refuse)
    with pytest.raises(OSError):
        await call("rename_note", {"old_filename": "alpha", "new_filename": "omega"})

    assert (config.BRAIN_DIR / "concepts" / "alpha.md").exists()
    res = await call("search_vault", {"query": "alpha explains node draining", "vault": "brain"})
    assert "alpha.md" in res[0].text


@pytest.mark.asyncio
async def test_rename_refuses_a_path_shaped_new_name(real_search_stack):
    """A rename keeps the note's folder (the folder carries its type), so a
    path-shaped name is refused up front rather than raising on a missing directory
    after the index has already been touched."""
    await call("write_note", {"vault": "brain", "filename": "alpha", "title": "Alpha",
                              "type_meta": "concept", "tags": [],
                              "content": "## AI Summary\n\nAlpha explains node draining."})

    res = await call("rename_note", {"old_filename": "alpha", "new_filename": "people/alpha.md"})
    assert "not a bare filename" in res[0].text
    assert not (config.BRAIN_DIR / "people").exists()
    assert (config.BRAIN_DIR / "concepts" / "alpha.md").exists()
    res = await call("search_vault", {"query": "alpha explains node draining", "vault": "brain"})
    assert "alpha.md" in res[0].text


@pytest.mark.asyncio
async def test_a_blocked_reindex_names_the_service_that_is_down(monkeypatch):
    """The reindex is the repair tool, so its refusal is read mid-outage.

    Three things have to be in it. WHICH service is down, because naming both as a pair
    sends the operator to verify one that is up. That the notes are intact, because the
    caller of a rebuild is already unsure what survived — the vault is the source of
    truth and a rebuild only ever reads from it. And that the call is worth repeating
    later, which is the whole next step and the one an error message usually omits.
    """
    import clients
    cases = [
        ({"embedder": None}, "the embedder", "Qdrant"),
        ({"qdrant_db": None}, "Qdrant", "the embedder"),
    ]
    for offline, named, running in cases:
        with monkeypatch.context() as m:
            m.setattr(clients, "embedder", _StubEmbedder(8))
            m.setattr(clients, "qdrant_db", object())
            for attr, value in offline.items():
                m.setattr(clients, attr, value)
            reply = (await call("reindex_vault", {}))[0].text
            assert named in reply, f"the reply does not name the offline service: {reply}"
            assert running not in reply, (
                f"the reply blames {running}, which is up: {reply}")
            assert "read_note" in reply, f"the reply says nothing that still works: {reply}"
            assert "retry" in reply, f"the reply does not say the rebuild can be repeated: {reply}"

    # Both down: neither may be dropped, or the operator fixes one and retries into the other.
    with monkeypatch.context() as m:
        m.setattr(clients, "embedder", None)
        m.setattr(clients, "qdrant_db", None)
        reply = (await call("reindex_vault", {}))[0].text
        assert "the embedder" in reply and "Qdrant" in reply, reply


# ----------------- reindex_vault runs one rebuild at a time -----------------
@pytest.mark.asyncio
async def test_two_concurrent_reindexes_embed_the_vault_once(monkeypatch, tmp_path):
    """A rebuild embeds the whole vault. A second concurrent run would pay that cost
    again to write the very points the first one is already writing, so it is refused
    instead of queued — the caller learns a rebuild is in flight."""
    import asyncio as _asyncio
    import clients
    monkeypatch.setattr(clients, "embedder", _StubEmbedder(8))
    monkeypatch.setattr(clients, "qdrant_db", _CountingQdrant())
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")   # absent → skipped
    for name in ("one", "two"):
        f = config.BRAIN_DIR / "concepts" / f"{name}.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"body of {name}", encoding="utf-8")

    seen = []

    def slow_index(f):
        seen.append(f)
        time.sleep(0.02)     # hold the lock long enough for the second call to land
        return True

    monkeypatch.setattr(vault, "index_markdown_file", slow_index)

    first, second = await _asyncio.gather(call("reindex_vault", {}), call("reindex_vault", {}))

    texts = sorted([first[0].text, second[0].text])
    assert "already running" in texts[0]
    assert "Reindexed 2 file(s)." in texts[1]
    assert len(seen) == 2          # each note embedded once, not twice


# ----------------- chunking -----------------
def test_chunking_long_doc():
    text = "x" * 5000
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert chunk_text("short") == ["short"]


def _unbroken(n):
    """`n` characters with no blank line and no repeat, so a duplicated span is
    detectable: a run of "x" would satisfy any substring check by accident."""
    return "".join(chr(ord("a") + (i * 7 + i // 26) % 26) for i in range(n))


def test_no_chunk_merely_repeats_its_predecessor():
    """A chunk that the previous one already contains costs an embedding call and a
    Qdrant point, and can win a search with a fragment too short to mean anything.

    Filling each chunk to the cap and letting the remainder become the last one is what
    produces those: at 1500/200 a 3901-character document ended in a single character.
    Sizes are derived from the configured cap so a config change cannot make this
    vacuous, and the multiples are what put the remainder near zero.
    """
    cap, step = config.CHUNK_SIZE_CHARS, config.CHUNK_SIZE_CHARS - config.CHUNK_OVERLAP_CHARS
    for length in (cap + 1, cap + step + 1, cap + 2 * step + 1, cap * 2, int(cap * 1.77)):
        chunks = chunk_text(_unbroken(length))
        assert len(chunks) > 1, f"{length} chars did not split at all"
        for i in range(1, len(chunks)):
            assert chunks[i] not in chunks[i - 1], (
                f"at {length} chars, chunk {i} ({len(chunks[i])} chars) adds nothing "
                f"to chunk {i - 1}: {chunks[i][:80]!r}")
        assert max(len(c) for c in chunks) <= cap, f"{length} chars exceeded the cap"


def test_chunks_break_where_the_document_does():
    """search prints the START of the chunk it matched (`_render_hit`), so the cut is
    also what the caller reads as evidence. A character offset lands mid-word — a real
    daily note produced a chunk opening on 'netes, Java.' — which reads as corruption
    and drops the list item or sentence that carried the meaning.

    The oversized paragraph is the case a naive paragraph split gets wrong: it has no
    blank line to cut at, and must still respect the cap that keeps an embedding
    request inside the model's input window.
    """
    doc = "\n\n".join(f"Paragraph {i} discusses draining nodes in some detail. " * 6
                      for i in range(60))
    chunks = chunk_text(doc)
    assert len(chunks) > 1, "fixture must produce several chunks"
    for chunk in chunks:
        body = chunk.split("\n\n", 1)[-1] if chunk.startswith("#") else chunk
        assert body.startswith("Paragraph "), f"chunk starts mid-paragraph: {body[:80]!r}"
        assert body.rstrip().endswith("detail."), f"chunk ends mid-sentence: {body[-80:]!r}"

    solid = "One long paragraph with no blank line anywhere in it. " * 90
    assert len(solid) > config.CHUNK_SIZE_CHARS * 2, "fixture must be several chunks long"
    for chunk in chunk_text(solid):
        assert len(chunk) <= config.CHUNK_SIZE_CHARS, (
            f"an unsplittable paragraph pushed a chunk to {len(chunk)} chars")


def test_every_chunk_names_the_note_and_section_it_came_from():
    """Only the first chunk of a note holds its H1 naturally, and none but the first of
    a section holds that heading — yet every chunk is retrieved on its own and rendered
    on its own. For a daily note, where the date IS the access axis, a chunk without it
    is prose the caller cannot place, and the embedding carries no hint of the topic the
    heading names either.
    """
    sections = ["Log", "Decisions", "Open questions"]
    doc = "# 2026-08-13\n"
    for name in sections:
        doc += f"\n## {name}\n\n"
        doc += "".join(f"- {name} item {i}, on the subject of node draining.\n"
                       for i in range(60))
    chunks = chunk_text(doc)
    assert len(chunks) > len(sections), "fixture must split sections further"

    for chunk in chunks:
        assert "# 2026-08-13" in chunk, f"chunk does not name its note: {chunk[:80]!r}"
        # Which sections this chunk carries content from, read off the items themselves
        # rather than the prefix, so the prefix is checked against the body and not
        # against itself.
        for name in sections:
            if f"- {name} item" in chunk:
                assert f"## {name}" in chunk, (
                    f"chunk holds {name} items but names no section: {chunk[:120]!r}")
