"""Issue #8: indexing failures reach the MCP client with file, error class and reason.

The client has no access to the server log, so "failed — see server log" leaves it
unable to act: it cannot tell a depleted embedding quota (wait, top up, retry) from a
file it should fix. Every failure is therefore classified as one short line —
`<kind>: <message>` plus whether a retry can help — and returned in the tool result.
"""
import base64
from unittest.mock import MagicMock

import httpx
import pytest
from google.genai import errors as genai_errors
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

import clients
import config
import registry
import vault


@pytest.fixture(autouse=True)
def _vaults(tmp_path, monkeypatch):
    brain, context, media = tmp_path / "brain-vault", tmp_path / "context-vault", tmp_path / "media"
    for d in (brain, context, media):
        d.mkdir(exist_ok=True)
    monkeypatch.setattr(config, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(config, "BRAIN_DIR", brain)
    monkeypatch.setattr(config, "CONTEXT_DIR", context)
    monkeypatch.setattr(config, "MEDIA_DIR", media)
    embedder = MagicMock()
    embedder.embed.side_effect = lambda items, **kw: [[0.1, 0.2, 0.3] for _ in items]
    monkeypatch.setattr(clients, "embedder", embedder)
    monkeypatch.setattr(clients, "qdrant_db", MagicMock())


def call(name, args=None):
    return registry.dispatch(name, args or {})


def _genai_error(code: int, status: str, message: str):
    cls = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
    return cls(code, {"error": {"code": code, "status": status, "message": message}})


def _embedder_raising(exc):
    class Broken:
        def embed(self, items, **kw):
            raise exc
    return Broken()


def _note(name="n.md", body="# N\n\n## AI Summary\nsome content"):
    path = config.BRAIN_DIR / "concepts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ----------------- classification -----------------

@pytest.mark.parametrize("code,status,retryable", [
    (402, "RESOURCE_EXHAUSTED", True),    # quota/credits: not the file's fault
    (429, "RESOURCE_EXHAUSTED", True),    # rate limit
    (408, "DEADLINE_EXCEEDED", True),
    (503, "UNAVAILABLE", True),
    (400, "INVALID_ARGUMENT", False),     # the request itself is wrong
    (403, "PERMISSION_DENIED", False),    # bad key: retrying changes nothing
])
def test_an_embedding_api_error_is_classified_with_code_and_status(monkeypatch, code, status, retryable):
    monkeypatch.setattr(clients, "embedder", _embedder_raising(_genai_error(code, status, "msg here")))
    failure = vault._try_index_markdown(_note())
    assert failure is not None
    assert failure.kind == "embedding"
    assert failure.retryable is retryable
    assert f"{code} {status}" in failure.message
    assert "msg here" in failure.message


def test_depleted_credits_say_so_and_that_a_plain_retry_will_not_help(monkeypatch):
    """402 is retryable (not a file problem), but retrying before topping up the
    prepaid credits fails the same way — the observed case from the issue."""
    monkeypatch.setattr(clients, "embedder", _embedder_raising(
        _genai_error(402, "RESOURCE_EXHAUSTED", "Your prepayment credits are depleted.")))
    failure = vault._try_index_markdown(_note())
    assert failure.retryable
    assert "credits depleted" in failure.message


def test_a_short_embedding_response_is_a_retryable_embedding_failure(monkeypatch):
    clients.embedder.embed.side_effect = lambda items, **kw: []
    failure = vault._try_index_markdown(_note())
    assert (failure.kind, failure.retryable) == ("embedding", True)


@pytest.mark.parametrize("exc", [
    UnexpectedResponse(500, "Internal Server Error", b"boom", httpx.Headers()),
    ResponseHandlingException(ConnectionRefusedError("Connection refused")),
])
def test_a_qdrant_failure_is_retryable(exc):
    clients.qdrant_db.upsert.side_effect = exc
    failure = vault._try_index_markdown(_note())
    assert (failure.kind, failure.retryable) == ("qdrant", True)


def test_an_unreadable_file_is_a_file_problem_not_retryable():
    path = _note()
    path.write_bytes(b"## AI Summary\n\xff\xfe broken utf-8")
    failure = vault._try_index_markdown(path)
    assert (failure.kind, failure.retryable) == ("read", False)


def test_an_unexpected_error_is_internal_and_not_retryable(monkeypatch):
    monkeypatch.setattr(clients, "embedder", _embedder_raising(RuntimeError("bad API key")))
    failure = vault._try_index_markdown(_note())
    assert (failure.kind, failure.retryable) == ("internal", False)
    assert "bad API key" in failure.message


def test_the_reason_is_one_short_line(monkeypatch):
    """A whole API response body or traceback would flood the client's context."""
    monkeypatch.setattr(clients, "embedder", _embedder_raising(
        RuntimeError("first line\n" + "x" * 2000)))
    message = vault._try_index_markdown(_note()).message
    assert "\n" not in message
    assert len(message) <= 200


def test_success_and_offline_skip_report_no_failure(monkeypatch):
    assert vault._try_index_markdown(_note()) is None
    monkeypatch.setattr(clients, "embedder", None)
    assert vault._try_index_markdown(_note()) is None


def test_the_bool_api_is_unchanged(monkeypatch):
    assert vault.index_markdown_file(_note()) is True
    monkeypatch.setattr(clients, "embedder", _embedder_raising(RuntimeError("x")))
    assert vault.index_markdown_file(_note()) is False


# ----------------- reindex_vault -----------------

@pytest.mark.asyncio
async def test_reindex_lists_each_failed_file_with_class_reason_and_retryable():
    _note("good.md")
    bad = _note("bad.md")
    bad.write_bytes(b"\xff\xfe")
    clients_embed = clients.embedder.embed.side_effect

    def embed(items, **kw):
        if any("rate-limited" in (i if isinstance(i, str) else i[-1]) for i in items):
            raise _genai_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded")
        return clients_embed(items, **kw)
    clients.embedder.embed.side_effect = embed
    _note("slow.md", "## AI Summary\nrate-limited note")

    out = (await call("reindex_vault"))[0].text
    assert "Reindexed 1 file(s). 2 failed:" in out
    assert "- brain-vault/concepts/bad.md: read:" in out
    assert "- brain-vault/concepts/slow.md: embedding: 429 RESOURCE_EXHAUSTED" in out
    assert "(retryable)" in out.split("slow.md")[1].splitlines()[0]
    assert "(retryable)" not in out.split("bad.md")[1].splitlines()[0]
    assert "server log" not in out


@pytest.mark.asyncio
async def test_reindex_caps_the_failure_list(monkeypatch):
    """A fully broken run must not flood the client: 20 entries, then a count."""
    monkeypatch.setattr(clients, "embedder", _embedder_raising(
        _genai_error(402, "RESOURCE_EXHAUSTED", "Your prepayment credits are depleted.")))
    for i in range(25):
        _note(f"n{i:02}.md")
    out = (await call("reindex_vault"))[0].text
    assert "25 failed:" in out
    assert sum(1 for line in out.splitlines() if line.startswith("- ")) == 20
    assert "... and 5 more" in out


@pytest.mark.asyncio
async def test_reindex_names_a_failed_media_file_by_its_folder():
    (config.MEDIA_DIR / "scan.pdf").write_bytes(b"%PDF-1.4")
    clients.qdrant_db.upsert.side_effect = ResponseHandlingException(ConnectionRefusedError("refused"))
    out = (await call("reindex_vault"))[0].text
    assert "- media/scan.pdf: qdrant:" in out


# ----------------- write tools -----------------

def _credits_depleted(monkeypatch):
    monkeypatch.setattr(clients, "embedder", _embedder_raising(
        _genai_error(402, "RESOURCE_EXHAUSTED", "Your prepayment credits are depleted.")))


def _assert_reason(out: str):
    assert "Indexing failed (embedding: 402 RESOURCE_EXHAUSTED" in out, out
    assert "retryable" in out
    assert "check server logs" not in out


_NOTE = {"vault": "brain", "filename": "x", "title": "X", "type_meta": "concept",
         "tags": [], "content": "## AI Summary\nContext here."}


@pytest.mark.asyncio
async def test_write_note_names_the_indexing_failure(monkeypatch):
    _credits_depleted(monkeypatch)
    out = (await call("write_note", _NOTE))[0].text
    assert "Wrote x.md" in out
    _assert_reason(out)


@pytest.mark.asyncio
async def test_append_to_note_names_the_indexing_failure(monkeypatch):
    await call("write_note", _NOTE)
    _credits_depleted(monkeypatch)
    out = (await call("append_to_note", {"filename": "x", "content": "- more"}))[0].text
    assert "Appended to x.md" in out
    _assert_reason(out)


@pytest.mark.asyncio
async def test_rename_note_names_the_indexing_failure(monkeypatch):
    await call("write_note", _NOTE)
    _credits_depleted(monkeypatch)
    out = (await call("rename_note", {"old_filename": "x", "new_filename": "y"}))[0].text
    _assert_reason(out)


@pytest.mark.parametrize("name,payload", [("doc.pdf", b"%PDF-1.4"), ("pic.png", b"\x89PNG\r\n")])
@pytest.mark.asyncio
async def test_upload_media_names_the_indexing_failure(monkeypatch, name, payload):
    _credits_depleted(monkeypatch)
    out = (await call("upload_media", {
        "filename": name, "content_base64": base64.b64encode(payload).decode()}))[0].text
    assert "Uploaded" in out
    _assert_reason(out)


@pytest.mark.asyncio
async def test_a_file_problem_says_to_fix_it_rather_than_retry(monkeypatch):
    await call("write_note", _NOTE)
    monkeypatch.setattr(clients, "embedder", _embedder_raising(
        _genai_error(400, "INVALID_ARGUMENT", "Request payload size exceeds the limit")))
    out = (await call("append_to_note", {"filename": "x", "content": "- more"}))[0].text
    assert "Indexing failed (embedding: 400 INVALID_ARGUMENT" in out
    assert "retryable" not in out
    assert "fix" in out.lower()
