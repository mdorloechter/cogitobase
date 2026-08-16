"""The vault write path: what lands on disk, where, and under whose name.

type == folder derivation, ambiguous basenames (the caller picks, not the server),
overwrite protection, append validating the whole resulting note, the server-owned
OKF frontmatter, autolinking and near-duplicate detection.
"""
import re

import frontmatter
import pytest

import config
import skills
import validation
import vault
from security import find_notes, vault_qualified_path
from conftest import PREAMBLE, call, _link_outside


class _FixedDatetime:
    """Minimal stand-in for vault.datetime so a test can pin 'now' to a date.

    vault.py does `datetime.now().strftime(...)`, so we only need now().strftime.
    """
    def __init__(self, ymd: str):
        self._ymd = ymd

    def now(self):
        outer = self

        class _N:
            def strftime(self, fmt):
                return outer._ymd
        return _N()


# ----------------- Vault core -----------------
@pytest.mark.asyncio
async def test_write_and_read_note():
    res = await call("write_note", {
        "vault": "brain", "filename": "test_note", "title": "My Title",
        "type_meta": "concept", "tags": ["testing"], "content": "This is a test.",
    })
    assert "Wrote test_note.md" in res[0].text
    res_read = await call("read_note", {"filename": "test_note.md"})
    assert "This is a test." in res_read[0].text
    assert "type: concept" in res_read[0].text
    assert "ai-first: true" in res_read[0].text
    assert "updated:" in res_read[0].text


# ----------------- Server-Enforcement B: type == folder -----------------
@pytest.mark.asyncio
async def test_write_note_derives_folder_from_type():
    """The server places a note in the folder its type_meta maps to (concept ->
    concepts/), and read_note finds it back by bare filename."""
    await call("write_note", {
        "vault": "brain", "filename": "kafka", "title": "Kafka",
        "type_meta": "tech", "tags": [], "content": "A log."})
    assert (config.BRAIN_DIR / "tech" / "kafka.md").exists()
    assert not (config.BRAIN_DIR / "kafka.md").exists()
    # Bare filename still resolves via the recursive basename search.
    res = await call("read_note", {"filename": "kafka"})
    assert "A log." in res[0].text


# ----------------- Ambiguous basenames: the caller picks, not the server -----------------
# Server-Enforcement B derives the folder from type_meta, so the folder IS the
# note's type — two notes may legitimately share a basename under different types.
# Resolving that by first-hit-wins meant directory iteration order decided which
# note was read, appended to, renamed or deleted.
def _ambiguous_vault():
    """The same basename as three different types, one of them in the other vault."""
    paths = {
        "concept": config.BRAIN_DIR / "concepts" / "roadmap.md",
        "project": config.BRAIN_DIR / "projects" / "roadmap.md",
        "standard": config.CONTEXT_DIR / "roadmap.md",
    }
    for type_meta, p in paths.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# Roadmap\nBODY-OF-{type_meta.upper()}", encoding="utf-8")
    return paths


def test_find_notes_returns_every_same_named_note():
    paths = _ambiguous_vault()
    assert sorted(find_notes("roadmap.md")) == sorted(paths.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args", [
    ("read_note", {}),
    ("append_to_note", {"content": "more text"}),
    ("delete_note", {}),
    ("rename_note", {"new_filename": "renamed.md"}),
])
async def test_ambiguous_name_is_refused_with_candidates(tool, args):
    """Every tool that addresses ONE note refuses an ambiguous name and names the
    alternatives — including read_note, because a silently-wrong read is what the
    agent then builds its next write on."""
    paths = _ambiguous_vault()
    key = "old_filename" if tool == "rename_note" else "filename"
    res = await call(tool, {key: "roadmap.md", **args})
    out = res[0].text

    assert "ambiguous" in out.lower()
    for p in paths.values():
        assert vault_qualified_path(p) in out, "each candidate must be listed"
    # No note was read, changed, moved or removed.
    for type_meta, p in paths.items():
        assert p.exists()
        assert p.read_text(encoding="utf-8") == f"# Roadmap\nBODY-OF-{type_meta.upper()}"
        assert f"BODY-OF-{type_meta.upper()}" not in out
    assert not (config.BRAIN_DIR / "concepts" / "renamed.md").exists()


@pytest.mark.asyncio
async def test_a_listed_candidate_resolves_when_resent():
    """The candidates are only useful if the server accepts them back verbatim, so
    resend each one and assert it reaches exactly that note."""
    paths = _ambiguous_vault()
    for type_meta, p in paths.items():
        res = await call("read_note", {"filename": vault_qualified_path(p)})
        assert f"BODY-OF-{type_meta.upper()}" in res[0].text
        for other in paths.values():
            if other != p:
                assert other.read_text(encoding="utf-8") not in res[0].text


@pytest.mark.asyncio
async def test_a_resent_candidate_mutates_only_that_note():
    """The write path, where picking the wrong note is destructive."""
    paths = _ambiguous_vault()
    target = paths["project"]
    res = await call("delete_note", {"filename": vault_qualified_path(target)})
    assert "Deleted" in res[0].text
    assert not target.exists()
    assert paths["concept"].exists(), "a same-named note must survive"
    assert paths["standard"].exists()


@pytest.mark.asyncio
async def test_a_unique_basename_still_resolves_without_a_path():
    """Control: the ambiguity check must not turn ordinary lookups into errors."""
    _ambiguous_vault()
    lone = config.BRAIN_DIR / "tech" / "kafka.md"
    lone.parent.mkdir(parents=True, exist_ok=True)
    lone.write_text("# Kafka\nBODY-OF-KAFKA", encoding="utf-8")
    res = await call("read_note", {"filename": "kafka"})
    assert "BODY-OF-KAFKA" in res[0].text


def test_a_vault_qualified_path_never_crosses_into_the_other_vault():
    """The qualified form exists to be unambiguous even when both vaults hold the
    same relative path, so each must resolve to its own vault only."""
    same = "shared.md"
    brain = config.BRAIN_DIR / same
    context = config.CONTEXT_DIR / same
    brain.write_text("brain body", encoding="utf-8")
    context.write_text("context body", encoding="utf-8")

    assert find_notes(f"{config.BRAIN_DIR.name}/{same}") == [brain]
    assert find_notes(f"{config.CONTEXT_DIR.name}/{same}") == [context]
    # Unqualified, the very same name is ambiguous.
    assert len(find_notes(same)) == 2


def test_a_vault_qualified_path_cannot_traverse_out():
    with pytest.raises(ValueError):
        find_notes(f"{config.BRAIN_DIR.name}/../../etc/passwd.md")


# A second note of the same name is legitimate — type_meta is explicit on every
# write and the folder keeps the two apart — but it costs the caller the ability
# to address either by bare name, so the write that incurs it says so.
@pytest.mark.asyncio
async def test_write_note_reports_a_same_named_note_of_another_type():
    args = {"vault": "brain", "filename": "roadmap", "title": "Roadmap", "tags": [],
            "content": "## AI Summary\nSummary line.\n\nBody text."}
    res = await call("write_note", {**args, "type_meta": "concept"})
    assert "Notices" not in res[0].text, "the first note of a name collides with nothing"

    res = await call("write_note", {**args, "type_meta": "project"})
    out = res[0].text
    assert "Wrote roadmap.md" in out, "the write itself must go through"
    assert "brain-vault/concepts/roadmap.md" in out
    assert "type 'concept'" in out
    assert (config.BRAIN_DIR / "projects" / "roadmap.md").exists()
    assert (config.BRAIN_DIR / "concepts" / "roadmap.md").exists()


@pytest.mark.asyncio
async def test_rename_note_reports_a_collision_it_creates():
    """Renaming INTO a name another type already holds is the other way this state
    arises, so it is reported the same way."""
    args = {"vault": "brain", "title": "T", "tags": [],
            "content": "## AI Summary\nSummary line.\n\nBody text."}
    await call("write_note", {**args, "filename": "roadmap", "type_meta": "concept"})
    await call("write_note", {**args, "filename": "plan", "type_meta": "project"})

    res = await call("rename_note", {"old_filename": "brain-vault/projects/plan.md",
                                     "new_filename": "roadmap.md"})
    out = res[0].text
    assert "Renamed to roadmap.md" in out
    assert "brain-vault/concepts/roadmap.md" in out
    assert "type 'concept'" in out
    assert (config.BRAIN_DIR / "projects" / "roadmap.md").exists()


@pytest.mark.asyncio
async def test_rename_note_names_the_notes_whose_links_it_orphans():
    """A rename rewrites no other note, so every [[old-name]] in the vault is left
    pointing at nothing. Obsidian resolves a wikilink case-insensitively and an
    alias/heading is not part of the target, so those forms are orphaned too and
    have to be reported — a link nobody is told about is a link nobody fixes."""
    args = {"vault": "brain", "title": "T", "tags": [], "type_meta": "concept",
            "content": "## AI Summary\nSummary line.\n\nBody text."}
    await call("write_note", {**args, "filename": "alpha"})
    await call("write_note", {**args, "filename": "cites",
                              "content": "## AI Summary\nS.\n\nSee [[alpha]]."})
    await call("write_note", {**args, "filename": "aliases",
                              "content": "## AI Summary\nS.\n\nSee [[Alpha|the one]]."})
    await call("write_note", {**args, "filename": "elsewhere",
                              "content": "## AI Summary\nS.\n\nNothing linked here."})

    res = await call("rename_note", {"old_filename": "alpha", "new_filename": "omega"})
    out = res[0].text
    assert "Renamed to omega.md" in out
    assert "2 note(s) still link to [[alpha]]" in out
    assert "brain-vault/concepts/cites.md" in out
    assert "brain-vault/concepts/aliases.md" in out
    assert "elsewhere" not in out          # not a link, not named
    assert "[[omega]]" in out              # says what to link to instead


@pytest.mark.asyncio
async def test_rename_note_stays_silent_when_nothing_links_to_the_old_name():
    """A warning on every rename is a warning nobody reads."""
    args = {"vault": "brain", "title": "T", "tags": [], "type_meta": "concept",
            "content": "## AI Summary\nSummary line.\n\nBody text."}
    await call("write_note", {**args, "filename": "alpha"})
    await call("write_note", {**args, "filename": "other"})

    out = (await call("rename_note", {"old_filename": "alpha",
                                      "new_filename": "omega"}))[0].text
    assert "Renamed to omega.md" in out
    assert "still link to" not in out
    assert "Notices:" not in out


@pytest.mark.asyncio
async def test_an_ambiguous_title_is_not_auto_linked(monkeypatch):
    """A [[title]] only resolves when one note carries it. With two, Obsidian — not
    the author — picks where the click lands, so the server names the candidates
    instead of inserting the link."""
    monkeypatch.setattr(config, "VAULT_AUTOLINK", "auto")
    args = {"vault": "brain", "title": "T", "tags": [],
            "content": "## AI Summary\nSummary line.\n\nBody text."}
    await call("write_note", {**args, "filename": "roadmap", "type_meta": "concept"})
    await call("write_note", {**args, "filename": "roadmap", "type_meta": "project"})
    await call("write_note", {**args, "filename": "kafka", "type_meta": "tech"})

    res = await call("write_note", {
        "vault": "brain", "filename": "notes", "title": "Notes", "type_meta": "concept",
        "tags": [], "content": "## AI Summary\nSummary line.\n\nSee roadmap and kafka."})
    out = res[0].text
    body = (config.BRAIN_DIR / "concepts" / "notes.md").read_text(encoding="utf-8")

    assert "[[roadmap]]" not in body, "an ambiguous title must not be linked"
    assert "[[kafka]]" in body, "a unique title still links"
    assert "'roadmap' matches several notes" in out
    assert "brain-vault/concepts/roadmap.md" in out
    assert "brain-vault/projects/roadmap.md" in out


def test_load_identity_skips_symlinked_context_files(tmp_path):
    """Identity text is fed to the model as authoritative instructions, so a link
    here is both a disclosure and an injection vector — and it is read on every
    session start without anyone asking for that file."""
    _link_outside(tmp_path, config.CONTEXT_DIR / "99-extra.md")
    (config.CONTEXT_DIR / "10-persona.md").write_text("I am the operator.", encoding="utf-8")
    identity = skills.load_identity()
    assert "HOSTFILE-SECRET-CONTENT" not in identity
    assert "I am the operator." in identity   # the real file still loads


def test_load_identity_drops_the_frontmatter_block():
    """`date`/`type`/`tags` are how the vault files a note, not something to instruct on.

    This block is pushed as authoritative rules on every session start, so a filing
    marker in it is context spent on text no reader can act on. The prose is what the
    operator wrote to be followed, and all of it must survive."""
    (config.CONTEXT_DIR / "20-rules.md").write_text(
        "---\ndate: '2026-07-17'\ntype: identity\ntags:\n- rules\nai-first: true\n---\n\n"
        "# Interaction Rules\n\nAnswer in German, code in English.\n", encoding="utf-8")
    identity = skills.load_identity()
    assert "## 20-rules.md" in identity, "the per-file heading still separates the files"
    assert "Answer in German, code in English." in identity
    for marker in ("ai-first:", "type: identity", "date:", "tags:"):
        assert marker not in identity, f"the frontmatter key {marker!r} reached the prompt"


def test_load_identity_keeps_a_body_that_opens_with_a_rule():
    """A `---` horizontal rule is not frontmatter, and the difference is load-bearing here.

    The parser reads that rule as an opening fence and returns everything up to the next
    one as metadata — which would drop the operator's first rule from the pushed context
    without failing anything. vault.split_note counts only a non-empty mapping as
    frontmatter, so the rule stays body."""
    (config.CONTEXT_DIR / "30-standards.md").write_text(
        "---\n\n# Standards\n\nNEVER force-push to main.\n\n---\n\nUse pytest.\n",
        encoding="utf-8")
    identity = skills.load_identity()
    assert "NEVER force-push to main." in identity
    assert "Use pytest." in identity


def test_load_identity_survives_unparseable_frontmatter():
    """Malformed YAML must cost the metadata, never the rules.

    An unquoted `: ` is easy to type in Obsidian and cannot be repaired from here. The
    whole file counts as body then, so the block shows up in the prompt — the wrong
    direction to fail in is losing identity text over a broken filing marker."""
    (config.CONTEXT_DIR / "40-broken.md").write_text(
        "---\ntype: identity: nested\n---\n\n# Broken\n\nAlways run the suite.\n",
        encoding="utf-8")
    identity = skills.load_identity()
    assert "Always run the suite." in identity


@pytest.mark.asyncio
async def test_the_identity_write_path_still_stores_frontmatter():
    """Stripping is for the pushed context only — the file on disk keeps its OKF block.

    The server owns that frontmatter (it is what makes the note machine-readable, and
    what append_to_note validates the type against), and read_note hands the file over
    verbatim. A strip that reached either would make the vault unfileable to save a
    handful of tokens in the prompt."""
    await call("write_note", {"vault": "context", "filename": "50-persona.md",
                              "title": "Persona", "type_meta": "identity", "tags": ["persona"],
                              "content": PREAMBLE + "\nI work in Ops."})
    on_disk = (config.CONTEXT_DIR / "50-persona.md").read_text(encoding="utf-8")
    assert on_disk.startswith("---"), "the note lost its server-owned frontmatter"
    assert frontmatter.loads(on_disk).metadata["type"] == "identity"

    res = await call("read_note", {"filename": "50-persona.md"})
    assert res[0].text.startswith("---"), "read_note must return the file as it stands"
    assert "type: identity" in res[0].text


def test_reindex_skips_symlinked_notes(tmp_path, monkeypatch):
    """Indexing egresses content to the embedding API and stores it in Qdrant, so a
    symlinked file must never be picked up — it would outlive the link itself."""
    _link_outside(tmp_path, config.BRAIN_DIR / "standard" / "indexme.md")
    real = config.BRAIN_DIR / "standard" / "real.md"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("real body", encoding="utf-8")

    seen = []
    monkeypatch.setattr(vault, "index_markdown_file", lambda f: seen.append(f) or True)
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")  # absent → skipped
    vault._reindex_all()

    assert [f.name for f in seen] == ["real.md"]


@pytest.mark.asyncio
async def test_write_note_ignores_misfiling_folder_in_filename():
    """A client-supplied folder prefix cannot override the type-derived folder —
    a type=concept note lands in concepts/, never the smuggled people/."""
    await call("write_note", {
        "vault": "brain", "filename": "people/sneaky.md", "title": "Sneaky",
        "type_meta": "concept", "tags": [], "content": "x"})
    assert (config.BRAIN_DIR / "concepts" / "sneaky.md").exists()
    assert not (config.BRAIN_DIR / "people" / "sneaky.md").exists()


@pytest.mark.asyncio
async def test_write_note_rejects_vault_type_mismatch():
    """type decides the vault: a context type in the brain vault (or vice versa)
    is a well-formedness error and is rejected without writing anything."""
    res = await call("write_note", {
        "vault": "brain", "filename": "who", "title": "Who",
        "type_meta": "identity", "tags": [], "content": "x"})
    assert "rejected" in res[0].text.lower()
    assert not (config.BRAIN_DIR / "who.md").exists()
    assert not (config.CONTEXT_DIR / "who.md").exists()
    # The mirror case: a brain type addressed to the context vault.
    res2 = await call("write_note", {
        "vault": "context", "filename": "note", "title": "Note",
        "type_meta": "concept", "tags": [], "content": "x"})
    assert "rejected" in res2[0].text.lower()


@pytest.mark.asyncio
async def test_write_note_context_type_lands_flat():
    """context-vault types (identity/standard) live flat, no subfolder."""
    await call("write_note", {
        "vault": "context", "filename": "me", "title": "Me",
        "type_meta": "identity", "tags": [], "content": "who I am"})
    assert (config.CONTEXT_DIR / "me.md").exists()


@pytest.mark.asyncio
async def test_append_note():
    await call("write_note", {
        "vault": "brain", "filename": "append_test", "title": "Append",
        "type_meta": "project", "tags": ["log"], "content": "Line 1.",
    })
    res = await call("append_to_note", {"filename": "append_test", "content": "Line 2."})
    assert "Appended to append_test.md" in res[0].text
    res_read = await call("read_note", {"filename": "append_test"})
    # A blank line between the blocks: Markdown joins two lines into one paragraph
    # otherwise, so a single newline is what glues an appended heading or list onto
    # whatever the note ended with.
    assert "Line 1.\n\nLine 2." in res_read[0].text


@pytest.mark.asyncio
async def test_write_note_emits_optional_status_and_supersedes():
    res = await call("write_note", {
        "vault": "brain", "filename": "proj", "title": "Proj",
        "type_meta": "project", "tags": [], "content": "## AI Summary\nx",
        "status": "active", "supersedes": "[[old-proj]]",
    })
    assert "Wrote" in res[0].text
    body = (await call("read_note", {"filename": "proj"}))[0].text
    assert "status: active" in body
    assert "supersedes: '[[old-proj]]'" in body or "supersedes: \"[[old-proj]]\"" in body


@pytest.mark.asyncio
async def test_write_note_omits_status_when_absent():
    await call("write_note", {
        "vault": "brain", "filename": "plain", "title": "Plain",
        "type_meta": "concept", "tags": [], "content": "## AI Summary\nx"})
    body = (await call("read_note", {"filename": "plain"}))[0].text
    assert "status:" not in body
    assert "supersedes:" not in body


@pytest.mark.asyncio
async def test_overwrite_preserves_date_and_refreshes_updated(monkeypatch):
    await call("write_note", {
        "vault": "brain", "filename": "evolve", "title": "Evolve",
        "type_meta": "concept", "tags": [], "content": "## AI Summary\nv1"})
    # Simulate a later write on a different day: the birth `date` must survive,
    # `updated` must move to the new day.
    monkeypatch.setattr(vault, "datetime", _FixedDatetime("2099-01-01"))
    await call("write_note", {
        "vault": "brain", "filename": "evolve", "title": "Evolve",
        "type_meta": "concept", "tags": [], "content": "## AI Summary\nv2",
        "overwrite": True})
    body = (await call("read_note", {"filename": "evolve"}))[0].text
    assert "updated: '2099-01-01'" in body or "updated: 2099-01-01" in body
    assert "date: '2099-01-01'" not in body and "date: 2099-01-01" not in body


@pytest.mark.asyncio
async def test_rename_and_delete_note():
    await call("write_note", {"vault": "brain", "filename": "old_name", "title": "T",
                              "type_meta": "project", "tags": [], "content": "C"})
    res_ren = await call("rename_note", {"old_filename": "old_name", "new_filename": "new_name"})
    assert "Renamed to new_name.md" in res_ren[0].text
    res_old = await call("read_note", {"filename": "old_name"})
    assert "not found" in res_old[0].text.lower()
    res_del = await call("delete_note", {"filename": "new_name"})
    assert "Deleted new_name.md" in res_del[0].text


# ----------------- Task 2: capture tools -----------------
@pytest.mark.asyncio
async def test_capture_session_retro_creates_then_appends():
    args = {"project": "MCP Server", "shipped": "capture tools",
            "worked": "delegation", "friction": "none"}
    res1 = await call("capture_session_retro", args)
    assert "Wrote" in res1[0].text
    path = config.BRAIN_DIR / "projects" / "mcp-server.md"
    assert path.exists()
    res2 = await call("capture_session_retro", {**args, "shipped": "tests"})
    assert "Appended" in res2[0].text
    body = path.read_text(encoding="utf-8")
    assert body.count("### Retro") == 2


@pytest.mark.asyncio
async def test_capture_session_retro_appends_when_another_type_shares_the_name():
    """The retro target is DERIVED (projects/<slug>.md), never in doubt — so a
    same-named note of another type must not turn the append into an ambiguity
    error and lose the retro."""
    args = {"project": "MCP Server", "shipped": "capture tools",
            "worked": "delegation", "friction": "none"}
    assert "Wrote" in (await call("capture_session_retro", args))[0].text
    decoy = config.BRAIN_DIR / "concepts" / "mcp-server.md"
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_text("# MCP Server\nDECOY-BODY", encoding="utf-8")

    res = await call("capture_session_retro", {**args, "shipped": "tests"})
    assert "Appended" in res[0].text
    path = config.BRAIN_DIR / "projects" / "mcp-server.md"
    assert path.read_text(encoding="utf-8").count("### Retro") == 2
    assert decoy.read_text(encoding="utf-8") == "# MCP Server\nDECOY-BODY"


@pytest.mark.asyncio
async def test_capture_inbox_writes_under_inbox_with_derived_title():
    res = await call("capture_inbox", {
        "note": "Look into replacing the sync worker with a debounced queue."})
    assert "Wrote" in res[0].text
    files = list((config.BRAIN_DIR / "inbox").glob("*.md"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    # Title derived from the note; OKF preamble auto-built so schema passes.
    assert "## AI Summary" in body
    assert "debounced queue" in body


async def _retro_note(project="MCP Server", **over):
    """Create the retro note and return its path."""
    args = {"project": project, "shipped": "capture tools",
            "worked": "delegation", "friction": "none", **over}
    assert "Wrote" in (await call("capture_session_retro", args))[0].text
    return config.BRAIN_DIR / "projects" / "mcp-server.md"


@pytest.mark.asyncio
async def test_a_retro_lands_in_the_retro_section_not_at_the_end_of_the_file():
    """The note's own summary says entries live under '## Retros'. Appending to the FILE
    files them under whatever section is last, so the first section the author adds below
    silently starts collecting retros — with the summary still claiming otherwise."""
    path = await _retro_note()
    path.write_text(path.read_text(encoding="utf-8")
                    + "\n## Open Questions\n- should we shard the index?\n", encoding="utf-8")

    res = await call("capture_session_retro", {
        "project": "MCP Server", "shipped": "SECOND", "worked": "w", "friction": "f"})
    assert "Appended" in res[0].text

    body = path.read_text(encoding="utf-8")
    retros, questions = body.index("## Retros"), body.index("## Open Questions")
    assert retros < body.index("SECOND") < questions
    # The author's own section survives the insert, heading and content.
    assert "- should we shard the index?" in body


@pytest.mark.asyncio
async def test_retros_stay_in_chronological_order_within_their_section():
    """Entries are appended at the END of the section, not the top: the summary promises
    oldest-first, and reversing it makes every note's own description wrong."""
    path = await _retro_note(shipped="FIRST")
    path.write_text(path.read_text(encoding="utf-8") + "\n## Notes\n- aside\n", encoding="utf-8")
    await call("capture_session_retro", {
        "project": "MCP Server", "shipped": "SECOND", "worked": "w", "friction": "f"})
    body = path.read_text(encoding="utf-8")
    assert body.index("FIRST") < body.index("SECOND")


@pytest.mark.asyncio
async def test_a_missing_retro_section_is_created_and_reported():
    """The author may have renamed the heading. Refusing would drop a retro at the end
    of a session, where nobody types it again — so the section is created, and said so,
    because a silent one leaves a duplicate section behind after a rename."""
    path = await _retro_note()
    body = path.read_text(encoding="utf-8").replace("## Retros", "## Session Log")
    path.write_text(body, encoding="utf-8")

    res = await call("capture_session_retro", {
        "project": "MCP Server", "shipped": "SECOND", "worked": "w", "friction": "f"})
    assert "Appended" in res[0].text
    assert "did not exist" in res[0].text
    body = path.read_text(encoding="utf-8")
    assert "## Retros" in body and "SECOND" in body
    assert body.index("## Retros") < body.index("SECOND")


@pytest.mark.asyncio
async def test_a_section_append_ignores_a_heading_inside_a_code_fence():
    """A '#' line in a fenced block is a shell comment, not a heading. Treating it as
    one ends the section mid-listing and splices the entry into the code."""
    await call("write_note", {
        "vault": "brain", "filename": "fenced", "title": "Fenced",
        "type_meta": "project", "tags": [], "content":
        "## AI Summary\nx\n\n## Log\n```bash\n# install\napt install foo\n```\n"})

    res = await call("append_to_note", {
        "filename": "fenced", "content": "- entry", "section": "## Log"})
    assert "did not exist" not in res[0].text
    body = (config.BRAIN_DIR / "projects" / "fenced.md").read_text(encoding="utf-8")
    assert body.index("apt install foo") < body.index("- entry")
    assert "```\n\n- entry" in body      # after the fence closes, not inside it


def _inbox_body():
    files = list((config.BRAIN_DIR / "inbox").glob("*.md"))
    assert len(files) == 1
    return files[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_capture_inbox_uses_the_summary_it_was_given():
    """Summarising is semantic work, which ADR 5 leaves to the client. When the client
    does it, that text is the preamble — not something derived alongside it."""
    await call("capture_inbox", {
        "note": "Long rambling note about the sync worker, queues, debouncing and more.",
        "summary": "Consider debouncing the sync worker."})
    body = _inbox_body()
    assert "## AI Summary\nConsider debouncing the sync worker." in body
    assert "excerpt" not in body.lower()
    assert "rambling note about the sync worker" in body      # the note itself is kept


@pytest.mark.asyncio
async def test_capture_inbox_marks_a_missing_summary_as_an_excerpt():
    """Without a summary the note carries its own opening, and says so. An unlabelled
    first-200-characters is indistinguishable from a real summary to whoever reads the
    note later — and it is the server asserting semantics it never derived."""
    note = ("The sync worker wakes on every write, which is wasteful. " * 8)
    await call("capture_inbox", {"note": note})
    body = _inbox_body()
    assert "No summary supplied" in body
    assert body.count("## Note") == 1                         # the full note is still there
    assert note.strip() in body


@pytest.mark.asyncio
async def test_capture_inbox_does_not_store_a_short_note_twice():
    """A note that fits in the excerpt is already printed under ## AI Summary; a
    ## Note section would put the same bytes in the file a second time. Longer than the
    60-char title cut, so the derived H1 is not itself a copy and the count is about the
    sections."""
    note = "Debounce the sync worker so it stops waking on every single write."
    assert 60 < len(note) <= 200
    await call("capture_inbox", {"note": note})
    body = _inbox_body()
    assert body.count(note) == 1
    assert "## Note" not in body


@pytest.mark.asyncio
async def test_capture_inbox_rejects_empty_note():
    res = await call("capture_inbox", {"note": "   "})
    assert "'note' is empty" in res[0].text


# ----------------- rename does not overwrite -----------------
@pytest.mark.asyncio
async def test_rename_no_overwrite():
    await call("write_note", {"vault": "brain", "filename": "a", "title": "A",
                              "type_meta": "concept", "tags": [], "content": "A"})
    await call("write_note", {"vault": "brain", "filename": "b", "title": "B",
                              "type_meta": "concept", "tags": [], "content": "B"})
    res = await call("rename_note", {"old_filename": "a", "new_filename": "b"})
    assert "already exists" in res[0].text
    res_read = await call("read_note", {"filename": "b"})
    assert "B" in res_read[0].text


# ----------------- Near-duplicate detection (_dedup_check) -----------------
_DUP_BODY = ("## AI Summary\n\nKubernetes drains a node before an upgrade so that "
             "pods reschedule without downtime for the workload.")


async def _write_dup_note(filename, **extra):
    """Write a note whose body is identical to every other note from this helper,
    so the deterministic stub embedder scores them at cosine ~1.0."""
    return await call("write_note", {
        "vault": "brain", "filename": filename, "title": filename.replace("-", " ").title(),
        "type_meta": "concept", "tags": ["k8s"], "content": _DUP_BODY, **extra})


@pytest.mark.asyncio
async def test_dedup_soft_threshold_warns_but_writes(real_search_stack):
    """At/above VAULT_DEDUP_SOFT the near-duplicate is reported, not blocked."""
    await _write_dup_note("drain-a")
    res = await _write_dup_note("drain-b")
    assert "Wrote drain-b.md" in res[0].text
    assert "Similar to existing note 'brain-vault/concepts/drain-a.md'" in res[0].text
    assert "consider append_to_note" in res[0].text
    assert (config.BRAIN_DIR / "concepts" / "drain-b.md").exists()


@pytest.mark.asyncio
async def test_dedup_hard_threshold_rejects_without_writing(real_search_stack, monkeypatch):
    """Above VAULT_DEDUP_HARD the write is refused — and the file must not appear,
    since the rejection returns before write_text."""
    monkeypatch.setattr(config, "VAULT_DEDUP_HARD", 0.5)
    await _write_dup_note("drain-a")
    res = await _write_dup_note("drain-b")
    assert "Rejected as near-duplicate of 'brain-vault/concepts/drain-a.md'" in res[0].text
    assert not (config.BRAIN_DIR / "concepts" / "drain-b.md").exists()


@pytest.mark.asyncio
async def test_dedup_excludes_the_note_being_written(real_search_stack, monkeypatch):
    """exclude_filename must keep a note from matching its own indexed vectors,
    otherwise every overwrite=true would be rejected as a duplicate of itself."""
    monkeypatch.setattr(config, "VAULT_DEDUP_HARD", 0.5)
    await _write_dup_note("drain-a")
    res = await _write_dup_note("drain-a", overwrite=True)
    assert "Wrote drain-a.md" in res[0].text
    assert "Rejected" not in res[0].text
    assert "Similar to existing note" not in res[0].text


@pytest.mark.asyncio
async def test_dedup_sees_a_same_named_note_of_another_type(real_search_stack):
    """The exclusion covers only the note being written. Two notes may share a
    basename under different types, so a basename-based exclusion would silently
    skip the duplicate — the very case dedup exists for."""
    await _write_dup_note("roadmap")                    # concept → concepts/roadmap.md
    res = await call("write_note", {
        "vault": "brain", "filename": "roadmap", "title": "Roadmap",
        "type_meta": "project", "tags": ["k8s"], "content": _DUP_BODY})
    assert "Similar to existing note 'brain-vault/concepts/roadmap.md'" in res[0].text


@pytest.mark.asyncio
async def test_the_reported_duplicate_resolves_as_sent(real_search_stack):
    """The 'consider append_to_note' hint must be actionable: the name the notice
    prints has to address exactly that note, even while another shares its basename."""
    await _write_dup_note("roadmap")
    res = await call("write_note", {
        "vault": "brain", "filename": "roadmap", "title": "Roadmap",
        "type_meta": "project", "tags": ["k8s"], "content": _DUP_BODY})
    dup = res[0].text.split("Similar to existing note '", 1)[1].split("'", 1)[0]
    appended = await call("append_to_note", {"filename": dup, "content": "MERGED-HERE"})
    assert "Appended" in appended[0].text
    assert "MERGED-HERE" in (config.BRAIN_DIR / "concepts" / "roadmap.md").read_text()


def test_dedup_check_degrades_when_index_offline(monkeypatch):
    """With no embedder/Qdrant the check reports 'no duplicate' instead of raising."""
    import clients
    monkeypatch.setattr(clients, "embedder", None)
    monkeypatch.setattr(clients, "qdrant_db", None)
    assert vault._dedup_check(_DUP_BODY, "brain", "brain-vault/concepts/x.md") == (None, 0.0)


# ----------------- mechanical autolink against known titles -----------------
def test_autolink_links_existing_note():
    out, linked = validation.autolink("Met with Project Phoenix team", ["Project Phoenix"])
    assert out == "Met with [[Project Phoenix]] team"
    assert linked == ["Project Phoenix"]


def test_autolink_is_idempotent():
    out, _ = validation.autolink("See [[Project Phoenix]] now", ["Project Phoenix"])
    assert out == "See [[Project Phoenix]] now"


def test_autolink_never_invents_links():
    out, linked = validation.autolink("Talk about Atlantis", ["Project Phoenix"])
    assert linked == [] and "[[" not in out


def test_suggest_links_reports_unlinked():
    s = validation.suggest_links("ping Alice and Bob", ["Alice", "Bob"])
    assert set(s) == {"Alice", "Bob"}


@pytest.mark.asyncio
async def test_write_note_autolinks_in_auto_mode(monkeypatch):
    monkeypatch.setattr(config, "VAULT_AUTOLINK", "auto")
    await call("write_note", {
        "vault": "brain", "filename": "existing-topic", "title": "Existing Topic",
        "type_meta": "concept", "tags": [], "content": PREAMBLE})
    res = await call("write_note", {
        "vault": "brain", "filename": "mention", "title": "Mention",
        "type_meta": "concept", "tags": [],
        "content": PREAMBLE + "\nRelated to existing-topic indeed."})
    saved = (config.BRAIN_DIR / "concepts" / "mention.md").read_text(encoding="utf-8")
    assert "[[existing-topic]]" in saved


# ----------------- overwrite protection + append validates whole note -----------------
@pytest.mark.asyncio
async def test_write_note_no_silent_overwrite():
    args = {"vault": "brain", "filename": "dup", "title": "Dup",
            "type_meta": "concept", "tags": [], "content": PREAMBLE + "\nv1"}
    await call("write_note", args)
    res = await call("write_note", {**args, "content": PREAMBLE + "\nv2"})
    assert "already exists" in res[0].text
    assert "v1" in (config.BRAIN_DIR / "concepts" / "dup.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_write_note_overwrite_true_replaces():
    args = {"vault": "brain", "filename": "dup2", "title": "Dup2",
            "type_meta": "concept", "tags": [], "content": PREAMBLE + "\nv1"}
    await call("write_note", args)
    await call("write_note", {**args, "content": PREAMBLE + "\nv2", "overwrite": True})
    assert "v2" in (config.BRAIN_DIR / "concepts" / "dup2.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_append_validates_resulting_note(monkeypatch):
    # In strict mode a note that loses (never had) its preamble can't be created;
    # but append must also guard. Create a valid note, then append in strict mode
    # something that keeps it valid -> ok; the guard path is exercised either way.
    await call("write_note", {
        "vault": "brain", "filename": "appendable", "title": "A",
        "type_meta": "concept", "tags": [], "content": PREAMBLE + "\nstart"})
    res = await call("append_to_note", {"filename": "appendable", "content": "more context"})
    assert "Appended" in res[0].text


# The vault is a git working tree Obsidian also writes to (ADR 4), so a note can
# legitimately exist without the server's `type` in its frontmatter. Validating the
# combined note against an empty type barred such a note from appends for good.
def _handwritten_note(folder, name, body):
    """Write a note the way a human or a git pull would: no server frontmatter."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.md"
    path.write_text(f"---\ntitle: {name}\n---\n\n{body}", encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_append_to_a_note_without_a_type_uses_its_folder():
    path = _handwritten_note(config.BRAIN_DIR / "concepts", "handwritten", PREAMBLE + "\nfrom obsidian")
    res = await call("append_to_note", {"filename": "handwritten", "content": "appended"})
    assert "Appended" in res[0].text, res[0].text
    saved = path.read_text(encoding="utf-8")
    assert "appended" in saved
    # The recovered type is persisted, so it is the note's own frontmatter that says
    # so from here on rather than a fallback re-derived on every append.
    assert "type: concept" in saved


@pytest.mark.asyncio
async def test_append_prefers_the_frontmatter_type_over_the_folder():
    """A note the server wrote keeps its declared type even if the folder disagrees
    (a hand-moved file must not be silently retyped by an append)."""
    (config.BRAIN_DIR / "people").mkdir(parents=True, exist_ok=True)
    path = config.BRAIN_DIR / "people" / "misfiled.md"
    path.write_text(f"---\ntitle: misfiled\ntype: concept\n---\n\n{PREAMBLE}\nbody",
                    encoding="utf-8")
    res = await call("append_to_note", {"filename": "misfiled", "content": "appended"})
    assert "Appended" in res[0].text, res[0].text
    assert "type: concept" in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_append_without_a_type_in_a_flat_vault_explains_the_cause():
    """identity and standard both live flat in the context-vault, so the folder cannot
    identify one. The rejection stands there, but must name why."""
    _handwritten_note(config.CONTEXT_DIR, "flat", PREAMBLE + "\nrules")
    res = await call("append_to_note", {"filename": "flat", "content": "appended"})
    assert "Append rejected" in res[0].text
    assert "no 'type' in its frontmatter" in res[0].text
    assert "write_note" in res[0].text


@pytest.mark.asyncio
async def test_append_to_a_note_with_unparseable_frontmatter_is_refused_by_name():
    """A block that means to be frontmatter but is not valid YAML — here the most common
    typo, an unquoted value containing ': '. The parser raised straight through the tool,
    so the client got a ScannerError naming neither the note nor a remedy. The file must
    stay untouched: a rewrite would either duplicate the block or drop it."""
    (config.BRAIN_DIR / "concepts").mkdir(parents=True, exist_ok=True)
    path = config.BRAIN_DIR / "concepts" / "badyaml.md"
    original = f"---\ntitle: My Note: A Subtitle\ntype: concept\n---\n\n{PREAMBLE}\nbody\n"
    path.write_text(original, encoding="utf-8")
    res = await call("append_to_note", {"filename": "badyaml", "content": "appended"})
    assert "Append rejected" in res[0].text, res[0].text
    assert "badyaml.md" in res[0].text and "not valid YAML" in res[0].text, res[0].text
    assert path.read_text(encoding="utf-8") == original, "the note was modified anyway"


@pytest.mark.asyncio
async def test_append_keeps_a_leading_horizontal_rule_and_its_text():
    """A note with NO frontmatter whose body OPENS with a `---` rule: the parser reads
    that rule as a frontmatter fence, reports no metadata, and swallows the text up to
    the closing rule. An append must not rewrite the file through it — the section
    between the rules is the author's writing and has to reach disk untouched."""
    (config.BRAIN_DIR / "concepts").mkdir(parents=True, exist_ok=True)
    path = config.BRAIN_DIR / "concepts" / "ruled.md"
    path.write_text(f"---\n\nTOP SECTION\n\n---\n\n# Ruled\n\n{PREAMBLE}\nbody\n",
                    encoding="utf-8")
    res = await call("append_to_note", {"filename": "ruled", "content": "appended"})
    assert "Appended" in res[0].text, res[0].text
    saved = path.read_text(encoding="utf-8")
    assert "TOP SECTION" in saved, f"the section between the rules was dropped:\n{saved}"
    assert "body" in saved and "appended" in saved, saved
    # It gained a frontmatter block (the note had none), and the rule stayed a rule.
    assert saved.startswith("---\n"), saved
    assert "type: concept" in saved and "updated:" in saved, saved


@pytest.mark.asyncio
async def test_append_leaves_obsidian_frontmatter_values_verbatim():
    """Values YAML reads as something other than what stands on disk (`10:30` → 630
    sexagesimal, `0123` → 83 octal, `1.10` → 1.1, `no` → false) must not be written
    back in their parsed form, and the key order must survive: an append re-serialising
    the block would silently rewrite metadata a human typed in Obsidian (ADR 4)."""
    (config.BRAIN_DIR / "concepts").mkdir(parents=True, exist_ok=True)
    path = config.BRAIN_DIR / "concepts" / "obsidian.md"
    block = ("type: concept\nstart: 10:30\nid: 0123\nver: 1.10\ndone: no\n"
             "related: [[Other Note]]\ntags:\n  - mytag\n")
    path.write_text(f"---\n{block}---\n\n# Obsidian\n\n{PREAMBLE}\nbody\n", encoding="utf-8")
    res = await call("append_to_note", {"filename": "obsidian", "content": "appended"})
    assert "Appended" in res[0].text, res[0].text
    saved = path.read_text(encoding="utf-8")
    for line in block.splitlines():
        assert line in saved, f"append rewrote {line!r}:\n{saved}"
    # The block's own lines keep their order; only `updated` is added.
    lines = saved.splitlines()
    fenced = lines[1:lines.index("---", 1)]
    assert [ln for ln in fenced if not ln.startswith("updated:")] == block.splitlines(), fenced


@pytest.mark.asyncio
async def test_append_refreshes_updated_without_duplicating_it():
    """The `updated` marker is set in place on every append, however often it runs —
    a second block, or a second `updated:` line, would make the note unparseable."""
    await call("write_note", {
        "vault": "brain", "filename": "touched", "title": "Touched",
        "type_meta": "concept", "tags": [], "content": PREAMBLE + "\nv1"})
    for n in range(3):
        await call("append_to_note", {"filename": "touched", "content": f"line {n}"})
    saved = (config.BRAIN_DIR / "concepts" / "touched.md").read_text(encoding="utf-8")
    assert saved.count("updated:") == 1, saved
    assert saved.count("type: concept") == 1, saved
    parsed = frontmatter.loads(saved)
    assert parsed.metadata["type"] == "concept", parsed.metadata
    assert all(f"line {n}" in parsed.content for n in range(3)), parsed.content


# ----------------- OKF frontmatter is server-owned -----------------
@pytest.mark.asyncio
async def test_okf_frontmatter_always_written():
    await call("write_note", {
        "vault": "brain", "filename": "okf", "title": "OKF Note",
        "type_meta": "decision", "tags": ["a", "", "  ", "b"], "content": PREAMBLE})
    saved = (config.BRAIN_DIR / "decisions" / "okf.md").read_text(encoding="utf-8")
    assert "type: decision" in saved
    assert "ai-first: true" in saved
    assert saved.count("# OKF Note") == 1  # exactly one H1, server-added
    # empty/whitespace tags were normalized away
    assert "- a" in saved and "- b" in saved


def _yaml_blocks(saved: str) -> int:
    """Count leading YAML frontmatter blocks: '---' fences at the top of the file."""
    count = 0
    lines = saved.splitlines()
    i = 0
    while i < len(lines) and lines[i].strip() == "---":
        end = next((j for j in range(i + 1, len(lines)) if lines[j].strip() == "---"), None)
        if end is None:
            break
        count += 1
        i = end + 1
        while i < len(lines) and not lines[i].strip():
            i += 1
    return count


# The server writes its own frontmatter around whatever body it is given, so a body
# that already carries one produces a note with two. README documents
# read_note → edit → write_note(overwrite=true), and read_note returns the file
# verbatim — so that loop grew one block per round.
@pytest.mark.asyncio
async def test_write_note_round_trip_keeps_one_frontmatter_block():
    args = {"vault": "brain", "filename": "loop", "title": "Loop",
            "type_meta": "concept", "tags": ["t"], "content": PREAMBLE + "\nv1"}
    await call("write_note", args)
    path = config.BRAIN_DIR / "concepts" / "loop.md"
    for round_ in range(2):
        body = (await call("read_note", {"filename": "loop"}))[0].text
        res = await call("write_note", {**args, "content": body + f"\nedit{round_}",
                                        "overwrite": True})
        assert "Wrote" in res[0].text, res[0].text
        assert _yaml_blocks(path.read_text(encoding="utf-8")) == 1, \
            f"round {round_} accumulated a second frontmatter block"
    saved = path.read_text(encoding="utf-8")
    assert "edit0" in saved and "edit1" in saved   # the edits survived the stripping
    assert "type: concept" in saved


@pytest.mark.asyncio
async def test_write_note_reports_the_frontmatter_it_dropped():
    """Silently discarding client metadata would hide the loss; name the keys."""
    res = await call("write_note", {
        "vault": "brain", "filename": "declared", "title": "Declared",
        "type_meta": "concept", "tags": [],
        "content": "---\ntype: person\nstatus: active\n---\n\n" + PREAMBLE})
    assert "Dropped the frontmatter block" in res[0].text
    assert "status" in res[0].text and "type" in res[0].text
    # Dropped, never merged: the client's `type: person` must not decide placement.
    assert (config.BRAIN_DIR / "concepts" / "declared.md").exists()


@pytest.mark.asyncio
async def test_write_note_adds_an_h1_only_when_the_body_has_none():
    """The H1 rule is presence, not count (ADR 5). A body without a heading gets the
    title prepended; a body that brings its own headings keeps exactly those, so two
    client H1s reach disk as two and the server's title appears nowhere."""
    await call("write_note", {
        "vault": "brain", "filename": "bare", "title": "Prepended Title",
        "type_meta": "concept", "tags": [], "content": PREAMBLE})
    bare = (config.BRAIN_DIR / "concepts" / "bare.md").read_text(encoding="utf-8")
    assert "# Prepended Title" in bare

    await call("write_note", {
        "vault": "brain", "filename": "twoh1", "title": "Server Title",
        "type_meta": "concept", "tags": [],
        "content": f"# First\n\n{PREAMBLE}\n\n# Second\n\nmore"})
    saved = (config.BRAIN_DIR / "concepts" / "twoh1.md").read_text(encoding="utf-8")
    body = saved.split("---", 2)[-1]   # past the server's frontmatter block
    assert len(re.findall(r"^#\s+\S", body, re.MULTILINE)) == 2, body
    assert "Server Title" not in body


@pytest.mark.asyncio
async def test_write_note_keeps_a_body_that_opens_with_a_horizontal_rule():
    """A '---' rule is not frontmatter. frontmatter.loads parses it as an empty block
    AND eats the text up to the closing rule, so that body must not be run through it."""
    body = "---\n\nIntro above the rule\n\n---\n\n" + PREAMBLE
    res = await call("write_note", {
        "vault": "brain", "filename": "ruled", "title": "Ruled",
        "type_meta": "concept", "tags": [], "content": body, "overwrite": True})
    assert "Wrote" in res[0].text, res[0].text
    saved = (config.BRAIN_DIR / "concepts" / "ruled.md").read_text(encoding="utf-8")
    assert "Intro above the rule" in saved
    assert "Dropped the frontmatter block" not in res[0].text
