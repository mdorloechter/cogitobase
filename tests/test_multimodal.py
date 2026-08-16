"""Tests for multimodal features (images, pdfs, media upload)."""
import base64
import os
import pytest
from unittest.mock import MagicMock

import config
import clients
import registry
import vault

@pytest.fixture(autouse=True)
def setup_teardown(tmp_path, monkeypatch):
    brain = tmp_path / "brain-vault"
    context = tmp_path / "context-vault"
    media = tmp_path / "media"
    for d in (brain, context, media):
        d.mkdir()
        
    monkeypatch.setattr(config, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(config, "BRAIN_DIR", brain)
    monkeypatch.setattr(config, "CONTEXT_DIR", context)
    monkeypatch.setattr(config, "MEDIA_DIR", media)
    
    # Mock clients
    mock_qdrant = MagicMock()
    mock_embedder = MagicMock()
    # One vector per input, like the real embedding API: index_markdown_file
    # refuses a batch whose length doesn't match its chunk count, so a mock with a
    # fixed-length return would reject every note it was handed.
    mock_embedder.embed.side_effect = lambda items, **kw: [[0.1, 0.2, 0.3] for _ in items]

    monkeypatch.setattr(clients, "qdrant_db", mock_qdrant)
    monkeypatch.setattr(clients, "embedder", mock_embedder)
    
def call(name, args=None):
    return registry.dispatch(name, args or {})

def test_index_strips_frontmatter_before_embedding():
    """The embedded/indexed text must be the note BODY only — YAML frontmatter
    (type/tags/date) must not pollute the vectors or the stored snippet."""
    md = config.BRAIN_DIR / "fm.md"
    md.write_text("---\ntype: concept\ntags: [x]\n---\n# Title\nprose body here",
                  encoding="utf-8")
    vault.index_markdown_file(md)
    embedded = clients.embedder.embed.call_args[0][0]
    text_sent = embedded[0] if isinstance(embedded[0], str) else embedded[0][-1]
    assert "prose body here" in text_sent
    assert "type: concept" not in text_sent          # frontmatter stripped
    assert "tags:" not in text_sent


def test_embed_falls_back_when_task_type_unsupported(monkeypatch):
    """vault._embed must degrade for embedders whose embed() has no task_type
    (e.g. the e2e stub), calling the plain signature instead of raising."""
    calls = {"with_tt": 0, "plain": 0}

    class StubEmbedder:
        def embed(self, items):   # NO task_type param
            calls["plain"] += 1
            return [[0.1, 0.2, 0.3] for _ in items]
    monkeypatch.setattr(clients, "embedder", StubEmbedder())
    out = vault._embed(["hello"], task_type="RETRIEVAL_DOCUMENT")
    assert calls["plain"] == 1 and len(out) == 1


@pytest.mark.asyncio
async def test_reindex_vault_reembeds_all_files():
    """reindex_vault must re-embed every markdown note AND every media file
    (image + PDF) — the documented recovery path after Qdrant data loss. A rebuild
    that skipped images would silently drop that whole content class."""
    (config.BRAIN_DIR / "a.md").write_text("# A\nalpha content", encoding="utf-8")
    (config.BRAIN_DIR / "b.md").write_text("# B\nbeta content", encoding="utf-8")
    (config.CONTEXT_DIR / "c.md").write_text("# C\ngamma content", encoding="utf-8")
    # Media: one image + one PDF — both must be re-indexed on recovery.
    (config.MEDIA_DIR / "pic.png").write_bytes(b"img-bytes")
    (config.MEDIA_DIR / "doc.pdf").write_bytes(b"pdf-bytes")

    res = await call("reindex_vault", {})
    assert "Reindexed 5 file(s)" in res[0].text          # 3 md + 1 image + 1 pdf
    assert clients.qdrant_db.upsert.call_count == 5


@pytest.mark.asyncio
async def test_reindex_vault_offline_is_reported():
    clients.embedder = None
    res = await call("reindex_vault", {})
    assert "offline" in res[0].text.lower()


@pytest.mark.asyncio
async def test_upload_media_success():
    # Valid base64 image
    b64_content = base64.b64encode(b"fake_image_data").decode("utf-8")
    res = await call("upload_media", {
        "filename": "test.png",
        "content_base64": b64_content
    })
    
    assert "Uploaded and indexed successfully" in res[0].text
    assert "![alt text](../media/test.png)" in res[0].text

    saved_file = config.MEDIA_DIR / "test.png"
    assert saved_file.exists()
    assert saved_file.read_bytes() == b"fake_image_data"
    # The standalone image is now indexed (not just saved).
    clients.qdrant_db.upsert.assert_called_once()

@pytest.mark.asyncio
async def test_upload_media_path_traversal():
    res = await call("upload_media", {
        "filename": "../../../etc/passwd",
        "content_base64": "YQ=="
    })
    assert "traversal" in res[0].text.lower() or "outside" in res[0].text.lower()

@pytest.mark.asyncio
async def test_upload_media_refuses_an_oversized_file_and_names_both_sizes(monkeypatch):
    """The caller sends base64 and never sees the decoded size, so 'too large' on its
    own leaves it unable to tell whether to shrink the file or raise the limit."""
    monkeypatch.setattr(config, "MAX_MEDIA_BYTES", 10)
    res = await call("upload_media", {
        "filename": "big.png",
        "content_base64": base64.b64encode(b"x" * 25).decode("utf-8"),
    })
    assert "too large" in res[0].text.lower()
    assert "25" in res[0].text and "10" in res[0].text
    assert not (config.MEDIA_DIR / "big.png").exists()


@pytest.mark.asyncio
async def test_the_upload_limit_does_not_move_with_the_note_text_limit(monkeypatch):
    """MAX_FILE_SIZE_BYTES bounds note TEXT. Deriving the media limit from it meant
    tightening the text limit silently shrank what could be uploaded — two unrelated
    quantities moving together, with nothing in either knob's name saying so."""
    monkeypatch.setattr(config, "MAX_FILE_SIZE_BYTES", 1)
    monkeypatch.setattr(config, "MAX_MEDIA_BYTES", 1024)
    res = await call("upload_media", {
        "filename": "small.png",
        "content_base64": base64.b64encode(b"x" * 500).decode("utf-8"),
    })
    assert "too large" not in res[0].text.lower()
    assert (config.MEDIA_DIR / "small.png").exists()


@pytest.mark.asyncio
async def test_upload_media_invalid_base64():
    res = await call("upload_media", {
        "filename": "test.png",
        "content_base64": "not_base64_!@#"
    })
    assert "not valid base64" in res[0].text

@pytest.mark.asyncio
async def test_upload_pdf_triggers_indexing():
    b64_content = base64.b64encode(b"fake_pdf_data").decode("utf-8")
    res = await call("upload_media", {
        "filename": "document.pdf",
        "content_base64": b64_content
    })
    
    assert "Uploaded and indexed PDF successfully" in res[0].text
    assert "[PDF](../media/document.pdf)" in res[0].text
    
    saved_file = config.MEDIA_DIR / "document.pdf"
    assert saved_file.exists()
    
    # Check if Qdrant upsert was called for the PDF
    clients.qdrant_db.upsert.assert_called_once()
    
    # Check if embedder was called with the Part
    call_args = clients.embedder.embed.call_args[0][0]
    assert len(call_args) == 1 # 1 document
    assert len(call_args[0]) == 2 # Part + Text
    assert "Document: document.pdf" in call_args[0][1]

def test_index_markdown_file_reports_failure(monkeypatch):
    """A broken embedder must make index_markdown_file return False and
    increment mcp_index_failures_total — not just log and vanish."""
    import observability

    class BrokenEmbedder:
        def embed(self, items, **kw):
            raise RuntimeError("bad API key")
    monkeypatch.setattr(clients, "embedder", BrokenEmbedder())

    md = config.BRAIN_DIR / "broken.md"
    md.write_text("# Broken\nsome content", encoding="utf-8")

    label = 'mcp_index_failures_total{kind="markdown"}'
    before = _counter_value(observability.metrics.render(), label)
    result = vault.index_markdown_file(md)
    after = _counter_value(observability.metrics.render(), label)

    assert result is False
    assert after == before + 1


@pytest.mark.asyncio
async def test_write_note_surfaces_indexing_failure(monkeypatch):
    """write_note must still save the note on an indexing failure, but tell the
    caller it isn't searchable yet instead of claiming plain success."""

    class BrokenEmbedder:
        def embed(self, items, **kw):
            raise RuntimeError("bad API key")
    monkeypatch.setattr(clients, "embedder", BrokenEmbedder())

    res = await call("write_note", {
        "vault": "brain", "filename": "note_x", "title": "X",
        "type_meta": "concept", "tags": [],
        "content": "## AI Summary\nContext here."})
    assert "Wrote note_x.md" in res[0].text
    assert "Indexing failed" in res[0].text
    assert (config.BRAIN_DIR / "concepts" / "note_x.md").exists()


@pytest.mark.asyncio
async def test_upload_media_reports_indexing_failure(monkeypatch):
    """upload_media must not claim 'indexed successfully' when indexing failed."""

    class BrokenEmbedder:
        def embed(self, items, **kw):
            raise RuntimeError("bad API key")
    monkeypatch.setattr(clients, "embedder", BrokenEmbedder())

    b64_content = base64.b64encode(b"fake_image_data").decode("utf-8")
    res = await call("upload_media", {
        "filename": "broken.png", "content_base64": b64_content})
    assert "indexing failed" in res[0].text.lower()
    assert "successfully" not in res[0].text.lower()
    assert (config.MEDIA_DIR / "broken.png").exists()  # file is still saved


def _counter_value(rendered: str, line_prefix: str) -> float:
    """Extract a metric's current value from Prometheus text (0.0 if absent)."""
    for line in rendered.splitlines():
        if line.startswith(line_prefix + " "):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def test_index_markdown_with_image(tmp_path, monkeypatch):
    # Create an image in the media dir
    img_path = config.MEDIA_DIR / "chart.jpg"
    img_path.write_bytes(b"image_bytes")
    
    # Create a markdown file linking to it
    md_path = config.BRAIN_DIR / "note.md"
    md_path.write_text("Here is a chart: ![chart](../media/chart.jpg)")
    
    # Call indexer directly
    vault.index_markdown_file(md_path)
    
    clients.qdrant_db.upsert.assert_called_once()
    
    # Inspect what went to the embedder
    embed_input = clients.embedder.embed.call_args[0][0]
    
    # embed_input should be a list of items. 
    # For a markdown file with an image, items[0] should be a list of Parts and Strings
    first_chunk_item = embed_input[0]
    
    # Should be a list because it has an image part + text
    assert isinstance(first_chunk_item, list)
    assert len(first_chunk_item) == 2  # 1 image part + 1 text chunk

    # The image Part must carry the REAL bytes and the correct mime type — not just
    # "some object was passed". This is what proves the image was actually inlined.
    part, text_chunk = first_chunk_item
    assert part.inline_data is not None
    assert part.inline_data.mime_type == "image/jpeg"
    assert part.inline_data.data == b"image_bytes"
    # The text chunk that accompanies the image is the note body.
    assert "Here is a chart" in text_chunk
