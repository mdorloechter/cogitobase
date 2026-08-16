"""Obsidian/OKF markdown vault tools + the Qdrant semantic index.

ADR 4: the .md files are the source of truth; Qdrant only holds a search index.
"""
import asyncio
import base64
import inspect
import re
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import frontmatter
from qdrant_client.models import (
    PointStruct, Filter, FieldCondition, MatchValue, IsEmptyCondition, PayloadField, Range,
)

import config
import clients
import validation
from config import log
from git_sync import enqueue_sync
from registry import register, text, OUTCOME_REJECTED, OUTCOME_UNAVAILABLE
from observability import metrics, timer

# Vault instruments.
_M_REJECT = metrics.counter(
    "mcp_vault_rejections_total", "Vault writes rejected, by rule.", ("rule",))
_M_EMBED = metrics.histogram(
    "mcp_embed_duration_seconds", "Embedding generation time in seconds.")
_M_INDEX_FAIL = metrics.counter(
    "mcp_index_failures_total", "Indexing failures by content type.", ("kind",))
from security import (validate_safe_path, find_notes, is_contained_file, point_id_for,
                      vault_relative_path, vault_qualified_path, chunk_text)


# Shared `filename` description for the tools that address ONE existing note.
_NAME_DESC = ("Bare filename (e.g. 'roadmap.md'), or — when several notes share that "
              "name — the vault-qualified path from the ambiguity response "
              "(e.g. 'brain-vault/projects/roadmap.md').")


def _resolve_note(filename: str, missing: str = "Note") -> tuple[Path | None, str | None]:
    """Resolve a client-supplied name to EXACTLY ONE note.

    Returns (path, None) on a unique hit, else (None, message). Two notes can
    legitimately share a basename because the folder — and therefore the type —
    differs (concepts/roadmap.md vs projects/roadmap.md). Picking one of them is
    not the server's call: the caller knows which context it means, so an
    ambiguous name is refused and the candidates are handed back as
    vault-qualified paths that find_notes accepts verbatim.

    `missing` names the thing that was not found, in the shape the skill tools already
    use: "<thing> '<name>' not found. <how to find one>". The name is echoed because a
    caller working from memory cannot otherwise tell a typo from a note that was never
    written, and the discovery tools are named because guessing again is the alternative.
    """
    matches = find_notes(filename)  # raises on traversal
    if not matches:
        return None, (f"{missing} '{filename}' not found. Use list_notes to see a vault's "
                      f"files, or search_vault to find one by content.")
    if len(matches) > 1:
        candidates = "\n".join(f"- {vault_qualified_path(p)}" for p in matches)
        return None, (
            f"'{filename}' is ambiguous — {len(matches)} notes share that name. "
            f"Resend with one of these exact paths:\n{candidates}")
    return matches[0], None


def _type_of(filepath: Path) -> str | None:
    """The note type a path's folder stands for, or None when the folder maps to
    no single type (the context-vault holds `identity` and `standard` flat)."""
    parent = filepath.parent.name
    types = [t for t, folder in config.TYPE_FOLDER.items() if folder == parent]
    return types[0] if len(types) == 1 else None


def _same_name_notice(filepath: Path) -> str | None:
    """Announce notes that already carry `filepath`'s basename elsewhere.

    Writing a second note of the same name is legitimate — the type is explicit on
    every write, and the folder keeps the two apart. It does carry a cost the
    caller should hear about at the moment it is incurred: a name shared by several
    notes addresses none of them, so read/append/rename/delete need the full path.
    """
    others = [p for p in find_notes(filepath.name) if p != filepath]
    if not others:
        return None
    listed = ", ".join(
        f"{vault_qualified_path(p)}" + (f" (type '{t}')" if (t := _type_of(p)) else "")
        for p in others)
    return (f"'{filepath.name}' now names {len(others) + 1} notes: {listed}. "
            f"Addressing any of them by bare name will be refused as ambiguous — "
            f"use the vault-qualified path.")


def _backlink_notice(old_stem: str, new_stem: str, renamed: Path) -> str | None:
    """Name the notes whose ``[[old_stem]]`` links a rename has just left dangling.

    Reported rather than repaired. Rewriting them would turn a one-file rename into
    a write across N notes with no way back if it fails halfway, and ``[[stem]]``
    only resolves when a single note carries that stem — so a blind rewrite can
    break a link that pointed at a different note. Naming them lets the caller fix
    the ones it means to.

    Matched case-insensitively, because that is how Obsidian resolves a wikilink.
    A link inside a fenced code block counts too: a false alarm costs a glance, a
    silent orphan costs the link.
    """
    hits, target = [], old_stem.casefold()
    for d in (config.BRAIN_DIR, config.CONTEXT_DIR):
        if not d.exists():
            continue
        for f in d.rglob("*.md"):
            if f == renamed or not is_contained_file(f, d):
                continue
            try:
                body = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Strip what follows the link target: an alias (`|Display`), a heading
            # (`#Section`) or a block ref (`^id`) — and any folder the link carries.
            if any(re.split(r"[|#^]", inner, 1)[0].strip().rsplit("/", 1)[-1].casefold()
                   == target for inner in validation._WIKILINK_RE.findall(body)):
                hits.append(vault_qualified_path(f))
    if not hits:
        return None
    hits.sort()
    listed = ", ".join(hits[:5]) + (f", +{len(hits) - 5} more" if len(hits) > 5 else "")
    return (f"{len(hits)} note(s) still link to [[{old_stem}]]: {listed}. A rename "
            f"rewrites no other note — update those links to [[{new_stem}]] yourself.")


def _known_titles(exclude_stem: str | None = None) -> tuple[list[str], list[str]]:
    """Filename stems of all existing notes, split into (unique, ambiguous).

    A stem is what Obsidian `[[…]]` resolves to, and it only resolves when exactly
    one note carries it. Since the folder encodes a note's type, two notes may hold
    the same stem under different types — `[[roadmap]]` then points at both, and
    Obsidian, not the author, decides where the click lands. Such stems are
    reported separately so they are never linked automatically.

    Cheap rglob over the vaults — the source of truth — so it works even when
    Qdrant is offline. The current note is excluded so it never links to itself.
    """
    counts: dict[str, int] = {}
    for d in (config.BRAIN_DIR, config.CONTEXT_DIR):
        if d.exists():
            for f in d.rglob("*.md"):
                if f.stem != exclude_stem and is_contained_file(f, d):
                    counts[f.stem] = counts.get(f.stem, 0) + 1
    unique = [t for t, n in counts.items() if n == 1]
    ambiguous = [t for t, n in counts.items() if n > 1]
    return unique, ambiguous


def _dedup_check(content: str, vault: str, exclude_path: str) -> tuple[str | None, float]:
    """Find the most similar EXISTING note. Returns (name, score) or (None, 0).

    Soft enforcement analogous to add_memory: we surface a near-duplicate, we do
    not silently merge. Degrades to (None, 0) when the index is unavailable.

    Both the returned name and `exclude_path` are vault-relative paths — the index's
    identity key. That makes the reported duplicate resolvable (the caller can act on
    the "consider append_to_note" hint), and it keeps the exclusion to the note being
    written: matching on the basename would also skip a *different* note of the same
    name, so concepts/roadmap.md would never see projects/roadmap.md as a duplicate.
    """
    if not clients.embedder or not clients.qdrant_db:
        return None, 0.0
    try:
        # Compare against the index using the SAME representation the index stores:
        # the first chunk of the frontmatter-stripped body, embedded as a DOCUMENT.
        # (A single per-note vector would blur signal across unrelated chunks in a
        # multi-chunk note.)
        probe_text = chunk_text(_body_without_frontmatter(content))[0]
        vec = _embed([probe_text], task_type="RETRIEVAL_DOCUMENT")[0]
        flt = Filter(must=[FieldCondition(key="vault", match=MatchValue(value=vault))])
        hits = clients.qdrant_db.query_points(
            collection_name=config.COLLECTION_NAME, query=vec, limit=3, query_filter=flt)
        points = hits.points if hasattr(hits, "points") else hits
        for p in points:
            # A point indexed without `path` can only be named by its basename.
            name = p.payload.get("path") or p.payload.get("filename")
            if name and name != exclude_path:
                return name, float(getattr(p, "score", 0.0) or 0.0)
    except Exception:
        log.exception("Dedup check failed (non-fatal).")
    return None, 0.0


def _ensure_h1_present(content: str, title: str) -> str:
    """Guarantee the note opens with a top-level heading (server-owned OKF structure).

    Only the ABSENCE of an H1 is fixed, by prepending the title. A body that already
    brings one is left as it is, extra H1s included — the server does not rewrite a
    client's heading structure, and prepending its own title on top of one would
    duplicate what the client already wrote. So the count is not enforced: a body with
    two H1s reaches disk with two (ADR 5).
    """
    if not validation._H1_RE.search(content):
        return f"# {title}\n\n{content}"
    return content


# The fence of a frontmatter block, matched with the same `-{3,}` rule the parser
# uses. Named groups so a rewrite can put the delimiters back exactly as found.
_FM_BLOCK_RE = re.compile(
    r"\A(?P<open>-{3,}[ \t]*\r?\n)(?P<block>.*?)(?P<close>^-{3,}[ \t]*\r?\n?)",
    re.DOTALL | re.MULTILINE)


class _NoteText(NamedTuple):
    """A note file split at its frontmatter block, kept as TEXT.

    Rewriting a note through the parser (load → edit → dumps) is lossy twice over: a
    leading `---` that is not frontmatter takes the text after it with it, and every
    value the block does carry is re-serialised as whatever YAML decided it meant.
    Holding the delimiters and the block's own text lets a rewrite put back every byte
    it did not deliberately change.
    """
    meta: dict               # the parsed block; {} when the file carries no frontmatter
    error: str | None        # a leading block is present but is not valid YAML
    fence: tuple[str, str]   # its two delimiter lines, verbatim ("" when absent)
    block: str               # the YAML text between them ("" when absent)
    body: str                # everything after it, byte-identical to the file


def split_note(raw: str) -> _NoteText:
    """Split a note file at its frontmatter block.

    A leading block counts as frontmatter ONLY when it parses to a non-empty mapping.
    A `---` horizontal rule, a block emptied out in Obsidian's properties panel, a
    comment-only block and a YAML list all present the same way to a parser — no
    mapping — and are treated as body, because that text is a human's writing and the
    server has no business reinterpreting it.
    """
    stripped = raw.lstrip()
    m = _FM_BLOCK_RE.match(stripped)
    if m is None:
        return _NoteText({}, None, ("", ""), "", raw)
    try:
        meta = frontmatter.loads(m.group(0)).metadata
    except Exception as e:
        # Malformed YAML. Not repairable from here, and any rewrite would either
        # duplicate the block or drop it — so the whole file stays body and the
        # caller decides what to tell the user.
        return _NoteText({}, " ".join(str(e).split()), ("", ""), "", raw)
    if not meta:
        return _NoteText({}, None, ("", ""), "", raw)
    return _NoteText(meta, None, (m.group("open"), m.group("close")),
                     m.group("block"), stripped[m.end():])


def _set_yaml_keys(block: str, updates: dict[str, str]) -> str:
    """Set each `key: value` in a frontmatter block's TEXT. Values go in verbatim.

    Line-based on purpose. Re-serialising the block would rewrite the values YAML read
    as something other than what stands on disk — `10:30` becomes 630, `0123` becomes
    83, `1.10` becomes 1.1, `no` becomes false — and would reorder every key on every
    append. An existing key is replaced in place, together with the continuation lines
    its old value spanned; a new one is appended after the last line.
    """
    lines = block.splitlines(keepends=True)
    pending = dict(updates)
    newline = "\r\n" if lines and lines[-1].endswith("\r\n") else "\n"
    out: list[str] = []
    i = 0
    while i < len(lines):
        key = next((k for k in pending if re.match(rf"{re.escape(k)}[ \t]*:", lines[i])), None)
        if key is None:
            out.append(lines[i])
            i += 1
            continue
        out.append(f"{key}: {pending.pop(key)}"
                   + ("\r\n" if lines[i].endswith("\r\n") else "\n"))
        i += 1
        while i < len(lines) and (lines[i][:1] in " \t" or lines[i].lstrip().startswith("- ")):
            i += 1
    out.extend(f"{k}: {v}{newline}" for k, v in pending.items())
    return "".join(out)


def _reassemble(note: _NoteText, body: str, updates: dict[str, str]) -> str:
    """Put a note file back together with `updates` applied to its frontmatter."""
    if note.block:
        return note.fence[0] + _set_yaml_keys(note.block, updates) + note.fence[1] + body
    # No frontmatter of its own: give it one, and keep the body one blank line below
    # the fence the way write_note lays a note out.
    block = "".join(f"{k}: {v}\n" for k, v in updates.items())
    return f"---\n{block}---\n" + ("" if body.startswith("\n") else "\n") + body


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _heading_level(line: str, in_fence: bool) -> int:
    """The ATX heading level of `line`, or 0 if it is not one.

    Lines inside a fenced code block never count: a shell snippet's `# install` is a
    comment, and reading it as a heading would end a section in the middle of a listing.
    """
    if in_fence:
        return 0
    m = _HEADING_RE.match(line)
    return len(m.group(1)) if m else 0


def _append_into_section(body: str, section: str, addition: str) -> tuple[str, bool]:
    """Append `addition` at the END of `section`, before whatever follows it.

    Returns (new body, whether the section had to be created). A section runs from its
    heading to the next heading of the same or a higher level — appending to the FILE
    instead files the text under whichever section happens to be last, which is a
    different section than the one asked for as soon as the note grows one.

    A missing section is created at the end rather than refused: the caller is a capture
    tool at the end of a session, and losing the entry costs more than a note whose
    layout the author changed. The first match wins if the heading appears twice.
    """
    want = section.strip()
    want_level = _heading_level(want, False)
    lines = body.splitlines()
    start, in_fence = None, False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if start is None:
            if not in_fence and line.strip() == want:
                start = i
            continue
        if 0 < _heading_level(line, in_fence) <= want_level:
            return "\n".join(_spliced(lines, i, addition)), False
    if start is None:
        return _joined(body, f"{want}\n{addition}"), True
    return "\n".join(_spliced(lines, len(lines), addition)), False


def _spliced(lines: list[str], at: int, addition: str) -> list[str]:
    """`lines` with `addition` inserted at index `at`, one blank line either side."""
    head = lines[:at]
    while head and not head[-1].strip():
        head.pop()          # absorb the blank lines already there, so they don't stack
    tail = lines[at:]
    return head + [""] + addition.rstrip("\n").splitlines() + ([""] if tail else []) + tail


def _joined(body: str, addition: str) -> str:
    """`addition` after `body`, separated by exactly one blank line.

    Markdown needs that blank line: `## Heading` on the line directly after a list item
    is still part of the paragraph to a renderer, so appending with a single newline
    silently glues two blocks into one.
    """
    return body.rstrip("\n") + "\n\n" + addition if body.strip() else addition


def _strip_client_frontmatter(content: str) -> tuple[str, list[str]]:
    """Drop a leading YAML block from a client-supplied body. Returns (body, keys).

    The server OWNS the OKF frontmatter and writes its own block around whatever body
    it is given, so a body that already carries one yields a note with TWO. The
    documented round-trip (read_note → edit → write_note overwrite=true) does exactly
    that: read_note returns the file verbatim, frontmatter and all, so each round adds
    another block.

    The block is DISCARDED, not merged: every field the server emits it computes
    itself (`date` is read back from the file on disk), so a client-supplied value
    could only overwrite a server-owned one. The returned keys are reported to the
    caller so the loss is visible rather than silent.

    Only strips when the block actually parsed as metadata — a body opening with a
    `---` horizontal rule leaves frontmatter.loads with empty metadata AND swallows
    the text up to the closing rule, so that content must be handed back untouched.
    """
    if not content.lstrip().startswith("---"):
        return content, []
    try:
        post = frontmatter.loads(content)
    except Exception:
        # Malformed YAML in the block — leave the body alone and let the validation
        # rules judge it. Not our job to repair what the client sent.
        return content, []
    if not post.metadata:
        return content, []
    return post.content, sorted(str(k) for k in post.metadata)


def _vault_of(filepath: Path) -> str:
    """Return 'context' or 'brain' for a given note path (keep identity separate)."""
    try:
        if filepath.resolve().is_relative_to(config.CONTEXT_DIR):
            return "context"
    except ValueError:
        pass
    return "brain"


# The inverse of config.TYPE_FOLDER, for reading a type back off an existing note's
# location. Only the folder-backed types are invertible: identity/standard both map
# to None (they live flat in the context-vault), so the flat case stays ambiguous.
_FOLDER_TYPE = {folder: t for t, folder in config.TYPE_FOLDER.items() if folder}


def _type_of_folder(filepath: Path) -> str:
    """Recover a note's type from the folder it sits in, or "" if that folder is not
    a type folder.

    The vault is a git working tree that Obsidian also writes to (ADR 4), so a note
    can exist without the server-written `type` in its frontmatter. Since the server
    DERIVES the folder from the type (Server-Enforcement B), the folder carries the
    same information and is authoritative for such a note.
    """
    return _FOLDER_TYPE.get(filepath.parent.name, "")


def _embedder_takes_task_type(embed_fn) -> bool:
    """Detect once (by signature) whether embed() accepts a task_type kwarg, so we
    never wrap the real call in a broad try/except that could swallow a genuine
    TypeError raised INSIDE embed()."""
    try:
        params = inspect.signature(embed_fn).parameters
    except (TypeError, ValueError):
        return False
    return "task_type" in params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


def _embed(items: list, task_type: str | None = None) -> list:
    """Call the embedder with an optional Gemini task_type, degrading gracefully
    for embedders (e.g. the test stub) whose embed() has no task_type parameter."""
    if task_type is not None and _embedder_takes_task_type(clients.embedder.embed):
        return list(clients.embedder.embed(items, task_type=task_type))
    return list(clients.embedder.embed(items))


def _body_without_frontmatter(raw: str) -> str:
    """Return the note body without YAML frontmatter, so index/search operate on
    prose only — frontmatter (type/tags/date) would otherwise pollute embeddings
    and leak into search snippets."""
    try:
        return frontmatter.loads(raw).content
    except Exception:
        return raw


def _drop_chunks_from(filepath: Path, first_idx: int) -> None:
    """Delete this note's points from chunk `first_idx` onwards.

    Point ids are derived from (path, chunk_idx), so re-upserting a note replaces
    its chunks in place — but a note that SHRINKS leaves the surplus tail behind,
    and those points keep serving the removed prose (plaintext payload included)
    to search. Runs after the upsert, so the note is never momentarily unfindable.
    """
    if not clients.qdrant_db:
        return
    clients.qdrant_db.delete(
        collection_name=config.COLLECTION_NAME,
        points_selector=Filter(must=[
            FieldCondition(key="path",
                           match=MatchValue(value=vault_relative_path(filepath))),
            FieldCondition(key="chunk_idx", range=Range(gte=first_idx)),
        ]),
    )


def index_markdown_file(filepath: Path) -> bool:
    """Chunk the document and upsert one point per chunk with a vault tag.

    Returns True if indexed (or skipped because embedder/Qdrant are offline —
    that state is already surfaced elsewhere), False if indexing was attempted
    and failed.
    """
    if not clients.embedder or not clients.qdrant_db:
        return True
    try:
        content = _body_without_frontmatter(filepath.read_text(encoding="utf-8"))
        chunks = chunk_text(content)
        vault = _vault_of(filepath)

        items = []
        img_pattern = re.compile(r'!\[.*?\]\((.*?)\)')
        for chunk in chunks:
            img_links = img_pattern.findall(chunk)
            parts = []
            for link in img_links:
                try:
                    img_path = (filepath.parent / link).resolve()
                    # Use is_relative_to (not str.startswith, which a sibling dir
                    # like "vault-data-evil/" could spoof) to confine reads to the vault.
                    if (img_path.exists() and img_path.is_file()
                            and img_path.is_relative_to(config.VAULT_ROOT)):
                        mime_type, _ = mimetypes.guess_type(img_path.name)
                        if mime_type and mime_type.startswith("image/"):
                            from google.genai import types
                            parts.append(types.Part.from_bytes(
                                data=img_path.read_bytes(),
                                mime_type=mime_type
                            ))
                except Exception:
                    pass
            if parts:
                parts.append(chunk)
                items.append(parts)
            else:
                items.append(chunk)

        with timer(_M_EMBED):
            embeddings = _embed(items, task_type="RETRIEVAL_DOCUMENT")
        if len(embeddings) != len(chunks):
            # zip() below would stop at the shorter side and index only the leading
            # chunks, so the tail of the note stays unfindable while the write
            # reports success. The embedding API returns one vector per input in
            # input order, so a mismatch is a broken response — and a partial index
            # is worse than none, because search then serves plausible-looking but
            # incomplete results. Fail instead: the callers already tell the user
            # the note is saved but not yet searchable.
            log.error("Embedder returned %d vectors for %d chunks in %s — refusing "
                      "to index a partial note.", len(embeddings), len(chunks), filepath)
            _M_INDEX_FAIL.inc(("markdown",))
            return False
        points = []
        # `path` is what identifies this note's points: two notes can share a
        # basename (a concept and a project both called roadmap.md), and deleting
        # one must not take the other's vectors with it.
        rel_path = vault_relative_path(filepath)
        for idx, (chunk, emb) in enumerate(zip(chunks, embeddings)):
            points.append(PointStruct(
                id=point_id_for(filepath, idx),
                vector=emb,
                payload={"filename": filepath.name, "vault": vault, "path": rel_path,
                         "chunk_idx": idx, "text": chunk},
            ))
        # Re-check containment right before the upsert, not just at the caller's
        # rglob: a reindex takes minutes of embedding latency, and the note may have
        # been deleted or replaced by a symlink meanwhile. Without this the rebuild
        # would republish the plaintext of a note that no longer exists.
        if not is_contained_file(filepath, config.VAULT_ROOT):
            log.info("Skipping index upsert for %s — no longer a contained file", filepath)
            return True
        clients.qdrant_db.upsert(collection_name=config.COLLECTION_NAME, points=points)
        # Then drop the surplus tail, so a shrunken note stops serving removed prose.
        _drop_chunks_from(filepath, len(chunks))
        return True
    except Exception:
        log.exception("Indexing failed for %s", filepath)
        _M_INDEX_FAIL.inc(("markdown",))
        return False


def index_pdf_file(filepath: Path) -> bool:
    """Index a PDF file natively using Gemini.

    Returns True if indexed (or skipped because embedder/Qdrant are offline),
    False if indexing was attempted and failed.
    """
    if not clients.embedder or not clients.qdrant_db:
        return True
    try:
        from google.genai import types
        part = types.Part.from_bytes(data=filepath.read_bytes(), mime_type="application/pdf")
        with timer(_M_EMBED):
            # Use RETRIEVAL_DOCUMENT like markdown/image indexing (asymmetric to the
            # RETRIEVAL_QUERY search vectors) for consistent ranking.
            embeddings = _embed([[part, f"Document: {filepath.name}"]], task_type="RETRIEVAL_DOCUMENT")
        
        emb = embeddings[0]
        points = [PointStruct(
            id=point_id_for(filepath, 0),
            vector=emb,
            payload={"filename": filepath.name, "vault": "media", "chunk_idx": 0, "text": f"PDF Document: {filepath.name}"}
        )]
        clients.qdrant_db.upsert(collection_name=config.COLLECTION_NAME, points=points)
        return True
    except Exception:
        log.exception("Indexing failed for PDF %s", filepath)
        _M_INDEX_FAIL.inc(("pdf",))
        return False


def index_image_file(filepath: Path) -> bool:
    """Index a standalone image natively using Gemini, so `vault='media'` image
    search returns uploaded images.

    Returns True if indexed (or skipped — offline, or not an image), False if
    indexing was attempted and failed.
    """
    if not clients.embedder or not clients.qdrant_db:
        return True
    try:
        mime_type, _ = mimetypes.guess_type(filepath.name)
        if not (mime_type and mime_type.startswith("image/")):
            return True
        from google.genai import types
        part = types.Part.from_bytes(data=filepath.read_bytes(), mime_type=mime_type)
        with timer(_M_EMBED):
            embeddings = _embed([[part, f"Image: {filepath.name}"]], task_type="RETRIEVAL_DOCUMENT")
        points = [PointStruct(
            id=point_id_for(filepath, 0),
            vector=embeddings[0],
            payload={"filename": filepath.name, "vault": "media", "chunk_idx": 0,
                     "text": f"Image: {filepath.name}"},
        )]
        clients.qdrant_db.upsert(collection_name=config.COLLECTION_NAME, points=points)
        return True
    except Exception:
        log.exception("Indexing failed for image %s", filepath)
        _M_INDEX_FAIL.inc(("image",))
        return False


def deindex_markdown_file(filepath: Path):
    """Remove all chunk points for one note from the index.

    Delete by a payload filter rather than a fixed range of chunk ids: a 2 MB note
    can produce ~1500 chunks, and any fixed range risks leaving orphaned vectors
    behind (ghost search hits) for notes beyond it.

    The filter matches the vault-relative path, so a note only ever loses its own
    vectors — matching on the basename would take every same-named note in the
    vault with it (concepts/roadmap.md and projects/roadmap.md are different
    notes). A point carrying no `path` cannot be matched that way, so a second
    branch falls back to (filename, vault); without it such a point would survive
    every delete as a permanent ghost hit.
    """
    if not clients.qdrant_db:
        return
    try:
        vault = _vault_of(filepath)
        flt = Filter(should=[
            Filter(must=[
                FieldCondition(key="path",
                               match=MatchValue(value=vault_relative_path(filepath))),
            ]),
            Filter(must=[
                FieldCondition(key="filename", match=MatchValue(value=filepath.name)),
                FieldCondition(key="vault", match=MatchValue(value=vault)),
                IsEmptyCondition(is_empty=PayloadField(key="path")),
            ]),
        ])
        clients.qdrant_db.delete(collection_name=config.COLLECTION_NAME, points_selector=flt)
    except Exception:
        log.exception("Deindexing failed for %s", filepath)


def _reindex_all() -> tuple[int, int]:
    """Rebuild the entire Qdrant index from the Markdown vault + media — the source
    of truth. Returns (indexed_files, failed_files). This is the supported recovery
    path after losing the Qdrant volume.

    A dimension change (EMBED_MODEL/EMBED_DIM) needs the collection dropped first —
    Qdrant refuses vectors of the wrong size, so every upsert here would fail."""
    indexed = failed = 0
    for d in (config.BRAIN_DIR, config.CONTEXT_DIR):
        if not d.exists():
            continue
        for f in d.rglob("*.md"):
            # Indexing sends the file's content to the embedding API and stores it
            # in Qdrant, so a file escaping the vault would be egressed and stay
            # searchable long after the link is gone.
            if not is_contained_file(f, d):
                continue
            if index_markdown_file(f):
                indexed += 1
            else:
                failed += 1
    if config.MEDIA_DIR.exists():
        for f in config.MEDIA_DIR.rglob("*"):
            if not is_contained_file(f, config.MEDIA_DIR):
                continue
            suffix = f.suffix.lower()
            # Mirror the upload_media write path: PDFs and images are both indexed,
            # so a rebuild must restore BOTH — indexing only PDFs would silently
            # drop every image vector on recovery.
            if suffix == ".pdf":
                indexer = index_pdf_file
            elif suffix in config.ALLOWED_MEDIA_EXTENSIONS:
                indexer = index_image_file
            else:
                continue
            if indexer(f):
                indexed += 1
            else:
                failed += 1
    return indexed, failed


# Serialises full rebuilds. A reindex embeds the whole vault, so two concurrent runs
# would double the API cost and the embedding latency for no gain — the second run
# writes the same points the first one already wrote.
_reindex_lock = asyncio.Lock()


@register(
    "reindex_vault",
    "Rebuild the semantic index from the Markdown vault (recovery after Qdrant data loss).",
    {"type": "object", "properties": {}},
)
async def reindex_vault(arguments: dict) -> list:
    # Name the service that is actually down, and say the notes survived. This is the
    # repair tool, so whoever reads this refusal is already diagnosing an outage: a reply
    # blaming both would send them to check a service that is up, and one that mentions
    # only the store leaves open whether the vault itself lost anything.
    down = [name for name, client in (("the embedder", clients.embedder),
                                      ("Qdrant", clients.qdrant_db)) if not client]
    if down:
        verb = "is" if len(down) == 1 else "are"
        return text(
            f"Cannot reindex: {' and '.join(down)} {verb} offline. The Markdown vault is "
            "the source of truth and is untouched — list_notes and read_note still work, "
            "and a rebuild only reads from disk, so retry this call once the service is up.",
            OUTCOME_UNAVAILABLE)
    if _reindex_lock.locked():
        return text("A reindex is already running. Wait for it to finish.", OUTCOME_REJECTED)
    async with _reindex_lock:
        indexed, failed = await asyncio.to_thread(_reindex_all)
    msg = f"Reindexed {indexed} file(s)."
    if failed:
        msg += f" {failed} file(s) failed — see server log."
    return text(msg)


_DEFAULT_SEARCH_LIMIT = 5
_MAX_SEARCH_LIMIT = 20


def _render_hit(p) -> str:
    """One hit as `File` / `Score` / `Snippet` lines.

    The score is what tells a real find from the top-k padding Qdrant always returns:
    a query with nothing relevant behind it still yields hits, and without a number
    they read exactly like the good ones. The client is required not to claim absence
    without an exhaustive search, so it needs the figure that reveals it.

    Score on its OWN line, never appended to the path: `File:` is a machine-read
    field — a caller passes that exact string back to read_note — so anything added
    to it corrupts the name. Absent when the point carries none (hand-built points,
    and any future non-scoring lookup) rather than printed as an empty value.
    """
    lines = [f"File: {p.payload.get('path') or p.payload['filename']}"]
    score = getattr(p, "score", None)
    if isinstance(score, (int, float)):
        lines.append(f"Score: {score:.3f}")
    lines.append(f"Snippet: {p.payload['text'][:300]}")
    return "\n".join(lines)


@register(
    "search_vault", "Semantic Search over vault. Each hit carries a relevance Score "
    "(0-1) — a low one means the index held nothing better, not that this is an answer.",
    {"type": "object", "properties": {
        "query": {"type": "string", "description": "What to look for, in natural language — "
                                                   "this is a semantic search, not a keyword match."},
        "vault": {"type": "string", "enum": ["brain", "context", "media", "all"],
                  "description": "Where to search (default 'brain'): 'context' for identity and "
                                 "standards, 'media' for images and PDFs, 'all' for everything."},
        "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_SEARCH_LIMIT,
                  "description": f"Hits to return (default {_DEFAULT_SEARCH_LIMIT})."}},
     "required": ["query"]},
)
async def search_vault(arguments: dict) -> list:
    if not clients.embedder or not clients.qdrant_db:
        return text("Cannot search: the embedder or Qdrant is offline. Notes on disk are "
                    "unaffected — list_notes and read_note still work.", OUTCOME_UNAVAILABLE)
    # Clamp rather than trust: the schema bound is advertised, not enforced by the
    # transport, and each hit costs a snippet in the caller's context. `or` would fold
    # an explicit 0 into the default and skip the floor.
    raw_limit = arguments.get("limit")
    try:
        requested = _DEFAULT_SEARCH_LIMIT if raw_limit is None else int(raw_limit)
    except (TypeError, ValueError):
        requested = _DEFAULT_SEARCH_LIMIT
    limit = max(1, min(requested, _MAX_SEARCH_LIMIT))
    # Embed the query as RETRIEVAL_QUERY (asymmetric to the RETRIEVAL_DOCUMENT
    # vectors in the index) for better ranking.
    query_vector = (await asyncio.to_thread(
        lambda: _embed([arguments["query"]], task_type="RETRIEVAL_QUERY")))[0]
    # Default search to brain; identity lives in context and is loaded via prompt/resources.
    vault = arguments.get("vault", "brain")
    query_filter = None
    if vault in ("brain", "context", "media"):
        query_filter = Filter(must=[FieldCondition(key="vault", match=MatchValue(value=vault))])
    hits = await asyncio.to_thread(
        clients.qdrant_db.query_points,
        collection_name=config.COLLECTION_NAME,
        query=query_vector,
        limit=limit,
        query_filter=query_filter,
    )
    points = hits.points if hasattr(hits, "points") else hits
    # Name each hit the way read_note takes it back unambiguously: the indexed
    # `path` is VAULT_ROOT-relative, which is exactly the vault-qualified form.
    # Media points carry no path — and no note to resolve — so they keep the
    # basename, as do points indexed without the field.
    results = [_render_hit(p) for p in points]
    return text("\n---\n".join(results) if results else "No matches found.")


@register(
    "list_notes", "List all files in a vault.",
    {"type": "object", "properties": {
        "vault": {"type": "string", "enum": ["brain", "context", "media"],
                  "description": "Which vault to list: 'brain' for notes, 'context' for identity "
                                 "and standards, 'media' for uploaded files."},
        "type_meta": {"type": "string", "enum": config.NOTE_TYPES, "description": "Optional filter by note type (e.g. 'person')."}
    },
     "required": ["vault"]},
)
async def list_notes(arguments: dict) -> list:
    vault = arguments["vault"]
    type_meta = arguments.get("type_meta")
    target_dir = {"brain": config.BRAIN_DIR, "context": config.CONTEXT_DIR,
                  "media": config.MEDIA_DIR}.get(vault, config.BRAIN_DIR)
                  
    if type_meta and type_meta in config.NOTE_TYPES:
        subfolder = config.TYPE_FOLDER[type_meta]
        if subfolder:
            target_dir = target_dir / subfolder

    if not target_dir.exists():
        return text("Vault empty.")
    # The media vault holds binaries (images/PDFs), the note vaults hold markdown.
    pattern = "*" if vault == "media" else "*.md"
    # Name each note the way read_note takes it back: the vault-qualified path,
    # matching search_vault. Bare basenames would list two same-named notes as the
    # same line twice, and neither of them would resolve. Media files address no
    # note, so they keep their basename.
    # is_contained_file, not is_file(): the latter follows a symlink planted in the
    # vault, listing a file the note tools then refuse to touch.
    files = [f.name if vault == "media" else vault_qualified_path(f)
             for f in sorted(target_dir.rglob(pattern))
             if is_contained_file(f, target_dir)]
    return text("\n".join(files) if files else "No files.")


@register(
    "read_note", "Read a note's full content, frontmatter included.",
    {"type": "object", "properties": {"filename": {"type": "string", "description": _NAME_DESC}},
     "required": ["filename"]},
)
async def read_note(arguments: dict) -> list:
    filepath, error = _resolve_note(arguments["filename"])  # raises on traversal
    if error:
        return text(error, OUTCOME_REJECTED)
    size = filepath.stat().st_size
    if size > config.MAX_FILE_SIZE_BYTES:
        return text(f"'{filepath.name}' is {size} bytes, over the {config.MAX_FILE_SIZE_BYTES} "
                    f"byte read limit (MAX_FILE_SIZE_BYTES). Search it with search_vault, "
                    f"which returns matching passages instead of the whole file.",
                    OUTCOME_REJECTED)
    return text(filepath.read_text(encoding="utf-8"))



@register(
    "write_note", "Create or overwrite an OKF/Obsidian note (git-persisted, shared across clients).",
    {"type": "object", "properties": {
        "vault": {"type": "string", "enum": ["brain", "context"],
                  "description": "Must match the type: 'context' for identity/standard, 'brain' otherwise."},
        "filename": {"type": "string",
                     "description": "Bare filename (e.g. 'jane-doe.md'). The folder is DERIVED from "
                                    "type_meta — any folder prefix here is ignored."},
        "title": {"type": "string", "description": "Human-readable title. It becomes the note's "
                                                   "top-level heading and the text other notes "
                                                   "link to as [[title]]."},
        "type_meta": {"type": "string", "enum": config.NOTE_TYPES,
                      "description": "What the note is about. Decides the folder it is filed in, "
                                     "and with it the vault."},
        "tags": {"type": "array", "items": {"type": "string"},
                 "description": "Frontmatter tags for Obsidian's own search. Pass an empty list "
                                "if none apply — semantic search does not need them."},
        "content": {"type": "string", "description":
                    "The note body in Markdown. Must open with a `## AI Summary` section; external "
                    "claims need a recency marker and a confidence level, and links to other notes "
                    "use [[wikilinks]]. A body that breaks a rule is refused with the reason."},
        "status": {"type": "string", "enum": ["active", "paused", "archived", "superseded"],
                   "description": "Optional lifecycle marker (mostly for project/decision/learning)."},
        "supersedes": {"type": "string",
                       "description": "Optional [[wikilink]] to the note this one replaces."},
        "overwrite": {"type": "boolean", "description": "Replace an existing note (default false)."}},
     "required": ["vault", "filename", "title", "type_meta", "tags", "content"]},
)
async def write_note(arguments: dict) -> list:
    vault = arguments["vault"]
    type_meta = arguments["type_meta"]
    # type == folder (Server-Enforcement B): the TYPE decides both the vault and
    # the subfolder, so placement can't drift from the taxonomy. For a known type
    # the vault is IMPLIED — a `vault` arg that contradicts it is a well-formedness
    # error (mechanically decidable, ADR 1), rejected up front. An unknown type has
    # no folder mapping, so it falls through to validate_note below, which rejects
    # it via the schema rule (with metrics) before anything is written.
    if type_meta in config.NOTE_TYPES:
        expected_vault = "context" if type_meta in config.CONTEXT_TYPES else "brain"
        if vault != expected_vault:
            return text(
                f"Note rejected — type '{type_meta}' belongs in the {expected_vault}-vault, "
                f"not '{vault}'. Set vault='{expected_vault}' (or change the type_meta).",
                OUTCOME_REJECTED)
        target_dir = config.CONTEXT_DIR if expected_vault == "context" else config.BRAIN_DIR
        subfolder = config.TYPE_FOLDER[type_meta]
        if subfolder:
            target_dir = target_dir / subfolder
    else:
        target_dir = config.BRAIN_DIR if vault == "brain" else config.CONTEXT_DIR
    # Only the basename is honoured — a client-supplied path (e.g. "decisions/x.md"
    # or a misfiling "people/x.md" under type=concept) can't override the derived
    # folder. A '..' component is a traversal attempt (not a mere folder hint), so
    # reject it loudly and consistently with read_note/find_notes.
    raw_filename = arguments["filename"]
    if ".." in Path(raw_filename).parts:
        raise ValueError("Security Error: Path traversal blocked")
    filename = Path(raw_filename).name
    if not filename.endswith(".md"):
        filename += ".md"
    filepath = validate_safe_path(target_dir, filename)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    title = arguments["title"]
    tags = [t.strip() for t in arguments.get("tags", []) if isinstance(t, str) and t.strip()]
    content = arguments["content"]
    content_bytes = len(content.encode("utf-8"))
    if content_bytes > config.MAX_FILE_SIZE_BYTES:
        return text(f"Content is {content_bytes} bytes, over the {config.MAX_FILE_SIZE_BYTES} "
                    f"byte limit (MAX_FILE_SIZE_BYTES). Split it across linked notes, or "
                    f"raise the limit on the server.", OUTCOME_REJECTED)
    content, dropped_meta = _strip_client_frontmatter(content)

    # Refuse to overwrite an existing note unless explicitly told to.
    #
    # KNOWN RACE (accepted): this check and the write below are separated by several
    # await points — _known_titles, and _dedup_check with its Gemini embedding call,
    # which is a network round-trip. Nothing serialises tool dispatch, so two
    # concurrent write_note calls for the same path can both see "does not exist",
    # both pass, and the later write silently replaces the earlier note. Both callers
    # are told "Wrote …".
    #
    # Accepted because the server is single-tenant: the only way to hit it is one
    # client issuing two writes for the same name concurrently, so the guard protects
    # against an accidental overwrite rather than against another party, and a git
    # remote keeps the replaced version in history. Closing it means creating the file
    # with O_EXCL ("x" mode) instead of asking exists(), which moves the write — and
    # therefore the "already exists" refusal — after the dedup call.
    if filepath.exists() and not arguments.get("overwrite", False):
        return text(f"'{filepath.name}' already exists. Pass overwrite=true to replace it, "
                    f"or use append_to_note / a new filename.", OUTCOME_REJECTED)

    # Run the enforcement pipeline on the body the client supplied.
    result = validation.validate_note(vault, arguments["type_meta"], tags, content, title)
    if not result.ok:
        for rule in (result.failed_rules or {"unknown"}):
            _M_REJECT.inc((rule,))
        log.warning("Vault write rejected", extra={
            "event": "vault_rejected", "rules": sorted(result.failed_rules), "file": filepath.name})
        msg = "Note rejected — fix the following and resend:\n- " + "\n- ".join(result.errors)
        return text(msg, OUTCOME_REJECTED)

    # Link mentions of EXISTING notes (mechanical, idempotent). Only titles that
    # identify ONE note are linked — a `[[…]]` matching several notes resolves
    # nowhere in particular, so those are named for the author to link by hand.
    notices = list(result.warnings)
    if dropped_meta:
        notices.append(
            f"Dropped the frontmatter block in your content ({', '.join(dropped_meta)}) — "
            f"the server owns that block and writes it itself. Send only the body; when "
            f"editing what read_note returned, strip its frontmatter first.")
    known, ambiguous = await asyncio.to_thread(_known_titles, filepath.stem)
    if config.VAULT_AUTOLINK == "auto":
        content, linked = validation.autolink(content, known)
        if linked:
            notices.append(f"Auto-linked existing notes: {', '.join('[[%s]]' % t for t in linked)}.")
    elif config.VAULT_AUTOLINK == "warn":
        suggestions = validation.suggest_links(content, known)
        if suggestions:
            notices.append("Mentions existing notes you could link: "
                           + ", ".join(f"[[{t}]]" for t in suggestions) + ".")
    if config.VAULT_AUTOLINK in ("auto", "warn"):
        for title in validation.suggest_links(content, ambiguous):
            paths = ", ".join(vault_qualified_path(p) for p in find_notes(f"{title}.md"))
            notices.append(f"Not auto-linked: '{title}' matches several notes ({paths}) — "
                           f"link the one you mean explicitly.")

    # Surface a near-duplicate (soft), or reject an almost-identical note (hard).
    dup_name, score = await asyncio.to_thread(
        _dedup_check, content, vault, vault_relative_path(filepath))
    if dup_name and score >= config.VAULT_DEDUP_HARD:
        return text(f"Rejected as near-duplicate of '{dup_name}' (similarity {score:.2f}). "
                    f"Use append_to_note/rename, or write_note with overwrite=true if intended.",
                    OUTCOME_REJECTED)
    if dup_name and score >= config.VAULT_DEDUP_SOFT:
        notices.append(f"Similar to existing note '{dup_name}' (similarity {score:.2f}) — "
                       f"consider append_to_note instead of a new note.")

    # Server OWNS the OKF frontmatter + single H1 — clients cannot forge it.
    content = _ensure_h1_present(content, title)
    today = datetime.now().strftime("%Y-%m-%d")
    # Preserve the original `date` (birth) on overwrite; `updated` always reflects
    # the last write. On a fresh note the two coincide.
    date = today
    if filepath.exists():
        try:
            date = frontmatter.load(filepath).metadata.get("date", today)
        except Exception:
            pass
    meta = {
        "date": date,
        "updated": today,
        "type": arguments["type_meta"],
        "tags": tags,
        "ai-first": True,
    }
    # Optional lifecycle fields (Lever C) — only emitted when the client supplies
    # them, so notes that don't need them stay clean. Not enforced (ADR 1).
    status = (arguments.get("status") or "").strip()
    if status:
        meta["status"] = status
    supersedes = (arguments.get("supersedes") or "").strip()
    if supersedes:
        meta["supersedes"] = supersedes
    post = frontmatter.Post(content, **meta)
    filepath.write_text(frontmatter.dumps(post), encoding="utf-8")
    indexed = await asyncio.to_thread(index_markdown_file, filepath)
    await enqueue_sync(f"MCP Bot: Wrote {filepath.name}")

    out = f"Wrote {filepath.name}"
    if (collision := await asyncio.to_thread(_same_name_notice, filepath)):
        notices.append(collision)
    if not indexed:
        notices.append(
            "Indexing failed — this note is saved but not yet searchable; "
            "check server logs or retry via reindex_vault.")
    if notices:
        out += "\nNotices:\n- " + "\n- ".join(notices)
    return text(out)


@register(
    "append_to_note", "Append text to an existing note, optionally at the end of one section "
    "(git-persisted, shared across clients).",
    {"type": "object", "properties": {"filename": {"type": "string", "description": _NAME_DESC},
                                      "content": {"type": "string", "description":
                                                  "Markdown to add. The RESULTING note is validated, "
                                                  "so the same rules as write_note apply to it."},
                                      "section": {"type": "string", "description":
                                                  "Heading to append under, e.g. '## Retros'. "
                                                  "Created at the end if the note has none. "
                                                  "Omit to append to the end of the note."}},
     "required": ["filename", "content"]},
)
async def append_to_note(arguments: dict) -> list:
    filepath, error = _resolve_note(arguments["filename"])
    if error:
        return text(error, OUTCOME_REJECTED)
    addition = arguments["content"]
    # Both parts are named: the caller knows what it is sending but not what the note
    # already holds, so a single total leaves it guessing which of the two to shrink.
    resulting = filepath.stat().st_size + len(addition.encode("utf-8"))
    if resulting > config.MAX_FILE_SIZE_BYTES:
        return text(f"'{filepath.name}' would grow to {resulting} bytes ({filepath.stat().st_size} "
                    f"already there plus {len(addition.encode('utf-8'))} appended), over the "
                    f"{config.MAX_FILE_SIZE_BYTES} byte limit (MAX_FILE_SIZE_BYTES). Append less, "
                    f"or split the note.", OUTCOME_REJECTED)

    # Anti-bypass: validate the RESULTING whole note, not just the delta, so
    # the structural rules can't be evaded by adding content piecemeal. We reuse
    # the existing note's frontmatter (vault/type/title) as validation context.
    existing = split_note(await asyncio.to_thread(filepath.read_text, encoding="utf-8"))
    if existing.error:
        # The note opens with a block that means to be frontmatter but is not valid
        # YAML — a colon inside an unquoted value, a tab indent, a leading '%'. All of
        # them are easy to type in Obsidian and none of them are repairable from here:
        # appending would leave the note carrying a block no reader can parse. Say which
        # note and what to do, because the parser's own message names neither.
        return text(f"Append rejected — the frontmatter of '{filepath.name}' is not valid "
                    f"YAML, so this note cannot be edited safely: {existing.error}\n\n"
                    f"Fix the block in the note itself (quote a value containing ': ', "
                    f"use spaces instead of tabs), then append.", OUTCOME_REJECTED)
    vault = _vault_of(filepath)
    # A note written in Obsidian or pulled from the mirror need not carry the
    # server's `type` — fall back to the folder, which encodes the same thing. Without
    # this the schema rule fails on an empty type and the note is barred from appends
    # for good, even though nothing about the append is wrong.
    type_meta = existing.meta.get("type") or _type_of_folder(filepath)
    title = filepath.stem
    # Build the resulting body ONCE and both validate and write that same string: two
    # separate expressions could drift, and then the note on disk is not the note the
    # rules passed. A `section` files the text at the end of that section instead of the
    # end of the file — the note's own summary names where its entries live, and the
    # file end stops being that place the moment the author adds a section below.
    section = (arguments.get("section") or "").strip()
    if section:
        combined, created = _append_into_section(existing.body, section, addition)
    else:
        combined, created = _joined(existing.body, addition), False
    result = validation.validate_note(vault, type_meta, [], combined, title)
    if not result.ok:
        for rule in (result.failed_rules or {"unknown"}):
            _M_REJECT.inc((rule,))
        log.warning("Append rejected", extra={
            "event": "vault_rejected", "rules": sorted(result.failed_rules), "file": filepath.name})
        msg = "Append rejected — the resulting note would be invalid:\n- " + "\n- ".join(result.errors)
        if not type_meta:
            # The folder gave us nothing: either it is not a type folder, or it is the
            # flat context-vault, where identity and standard are indistinguishable.
            # Name the cause, because "Invalid type_meta ''" reads like a bug in the
            # request when the request never mentioned a type.
            msg += (f"\n\nThis note has no 'type' in its frontmatter and its folder "
                    f"('{filepath.parent.name}') does not identify one. Set the type with "
                    f"write_note (overwrite=true), then append.")
        return text(msg, OUTCOME_REJECTED)

    # Append the text, then refresh the `updated` marker so the frontmatter reflects
    # the last touch. Everything the append does not touch — the body, the block's key
    # order, each value exactly as written — is carried over as text, so a note edited
    # in Obsidian comes back out of an append the way its author left it (ADR 4).
    updates = {"updated": datetime.now().strftime("%Y-%m-%d")}
    # Record the type we validated against, so it is the note's own frontmatter that
    # says so from here on rather than a fallback re-derived on every append.
    if existing.meta.get("type") != type_meta:
        updates["type"] = type_meta
    filepath.write_text(_reassemble(existing, combined, updates), encoding="utf-8")
    indexed = await asyncio.to_thread(index_markdown_file, filepath)
    await enqueue_sync(f"MCP Bot: Appended to {filepath.name}")
    out = f"Appended to {filepath.name}"
    if section:
        out += f" under '{section}'"
    notices = list(result.warnings)
    if created:
        # The author renamed or removed the heading, so the note's layout is not what
        # the caller assumed. Creating it beats losing the text, but silently doing so
        # would leave a second section behind whenever the author only renamed one.
        notices.append(f"'{section}' did not exist in this note — it was added at the end.")
    if not indexed:
        notices.append(
            "Indexing failed — this note is saved but not yet searchable; "
            "check server logs or retry via reindex_vault.")
    if notices:
        out += "\nNotices:\n- " + "\n- ".join(notices)
    return text(out)


@register(
    "rename_note", "Rename a note, keeping its folder (git-persisted, shared across clients). "
    "Incoming [[wikilinks]] are NOT rewritten — the notes holding one are named in the reply, "
    "for you to fix with append_to_note or write_note.",
    {"type": "object", "properties": {
        "old_filename": {"type": "string", "description": _NAME_DESC},
        "new_filename": {"type": "string", "description": "Bare new filename; the note keeps its folder."}},
     "required": ["old_filename", "new_filename"]},
)
async def rename_note(arguments: dict) -> list:
    old_path, error = _resolve_note(arguments["old_filename"], missing="Note to rename")
    if error:
        return text(error, OUTCOME_REJECTED)
    new_filename = arguments["new_filename"]
    # A rename only changes the NAME — the note keeps its folder, because the folder
    # encodes its type. A path-shaped name would target a directory that need not
    # exist, so refuse it here rather than let the rename raise.
    if len(Path(new_filename).parts) > 1:
        return text(f"'{new_filename}' is not a bare filename. A rename keeps the note's "
                    f"folder (the folder carries its type) — pass a name without a path.",
                    OUTCOME_REJECTED)
    if not new_filename.endswith(".md"):
        new_filename += ".md"
    new_path = validate_safe_path(old_path.parent, new_filename)
    if new_path.exists():  # don't silently overwrite
        return text(f"Target '{new_path.name}' already exists; rename aborted.", OUTCOME_REJECTED)
    # Rename FIRST, then move the index: a failed rename must leave the note exactly
    # as it was. Deindexing up front would strip a note that is still on disk of
    # every vector, making it silently unsearchable until the next reindex.
    old_path.rename(new_path)
    deindex_markdown_file(old_path)
    indexed = await asyncio.to_thread(index_markdown_file, new_path)
    await enqueue_sync(f"MCP Bot: Renamed to {new_path.name}")
    out = f"Renamed to {new_path.name}"
    notices = []
    if (collision := await asyncio.to_thread(_same_name_notice, new_path)):
        notices.append(collision)
    if (orphans := await asyncio.to_thread(_backlink_notice, old_path.stem,
                                          new_path.stem, new_path)):
        notices.append(orphans)
    if not indexed:
        notices.append("Indexing failed — this note is saved but not yet "
                       "searchable; check server logs or retry via reindex_vault.")
    if notices:
        out += "\nNotices:\n- " + "\n- ".join(notices)
    return text(out)


@register(
    "delete_note", "Delete a note and drop it from the semantic index (git-persisted, shared "
    "across clients — the deletion is committed to the mirror). Not undoable from here; a copy "
    "survives only in git history.",
    {"type": "object", "properties": {"filename": {"type": "string", "description": _NAME_DESC}},
     "required": ["filename"]},
)
async def delete_note(arguments: dict) -> list:
    filepath, error = _resolve_note(arguments["filename"])
    if error:
        return text(error, OUTCOME_REJECTED)
    deindex_markdown_file(filepath)
    filepath.unlink()
    await enqueue_sync(f"MCP Bot: Deleted {filepath.name}")
    return text(f"Deleted {filepath.name}")


@register(
    "upload_media", "Upload a binary file (image, PDF) to the vault's media directory, where it "
    "is indexed for search (git-persisted, shared across clients). Requires base64 encoded content.",
    {"type": "object", "properties": {
        "filename": {"type": "string", "description": "Name of the file with extension (e.g. diagram.png)"},
        "content_base64": {"type": "string", "description": "Base64 encoded binary content"}},
     "required": ["filename", "content_base64"]},
)
async def upload_media(arguments: dict) -> list:
    filename = arguments["filename"]
    content_b64 = arguments["content_base64"]
    
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    
    # Path traversal protection
    try:
        filepath = validate_safe_path(config.MEDIA_DIR, filename)
    except ValueError as e:
        return text(str(e), OUTCOME_REJECTED)

    # Restrict to the media types the index actually understands (images + PDF).
    # Rejecting arbitrary binaries keeps the media vault from becoming a dumping
    # ground for executables/archives the server would never index or serve.
    if filepath.suffix.lower() not in config.ALLOWED_MEDIA_EXTENSIONS:
        return text(
            f"Unsupported media type '{filepath.suffix}'. Allowed: "
            f"{', '.join(sorted(config.ALLOWED_MEDIA_EXTENSIONS))}.", OUTCOME_REJECTED)

    try:
        data = base64.b64decode(content_b64)
    except Exception:
        return text("'content_base64' is not valid base64. Send the file's raw bytes in "
                    "standard base64 encoding, not the file's text or a data: URL.",
                    OUTCOME_REJECTED)
        
    # Name both numbers: the caller cannot see the decoded size it produced, so
    # "too large" alone leaves it guessing whether to shrink the file or raise a limit.
    if len(data) > config.MAX_MEDIA_BYTES:
        return text(f"File too large: {len(data)} bytes decoded, limit is "
                    f"{config.MAX_MEDIA_BYTES} (MAX_MEDIA_BYTES).", OUTCOME_REJECTED)
        
    filepath.write_bytes(data)
    await enqueue_sync(f"MCP Bot: Uploaded media {filepath.name}")
    
    if filepath.suffix.lower() == ".pdf":
        indexed = await asyncio.to_thread(index_pdf_file, filepath)
        if not indexed:
            return text(f"Uploaded, but indexing failed: {filepath.name}. The file is saved "
                        f"but not searchable yet; check server logs or retry via reindex_vault.")
        return text(f"Uploaded and indexed PDF successfully: {filepath.name}. You can link it using: [PDF](../media/{filepath.name})")

    # Standalone images are indexed too, so `search_vault(vault='media')` can
    # find uploaded images.
    indexed = await asyncio.to_thread(index_image_file, filepath)
    if not indexed:
        return text(f"Uploaded, but indexing failed: {filepath.name}. The file is saved "
                    f"but not searchable yet; check server logs or retry via reindex_vault.")
    return text(f"Uploaded and indexed successfully. You can link it in markdown using: ![alt text](../media/{filepath.name})")
