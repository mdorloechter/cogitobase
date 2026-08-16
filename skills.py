"""Identity / Skills layer (ADR 3) — the always-on "context engine".

The context-vault holds who-you-are, coding standards and AI rules. Unlike the
brain vault (pull/on-demand) this is push/always-on, over two channels (ADR 14):
the `get_core_context` tool returns the whole thing live, and the MCP
`instructions` field carries a trigger telling the client to call it on connect.
No MCP resources are exposed — a resource-iterating client would bypass the
"catalog push, body pull" split that keeps skill bodies out of every session
(see server.py's note above list_tools).

This module holds the pure logic; server.py wires it to MCP decorators.
"""
import asyncio
import hashlib

import frontmatter

import config
import vault
from config import log
from security import is_contained_file, validate_safe_path
from git_sync import enqueue_sync
from registry import register, text, OUTCOME_REJECTED

PROMPT_NAME = "agentic_second_brain"

AGENTIC_META_PROMPT = """You are interacting with a specialized environment where my personal profile, architectural guidelines, and second brain are available via MCP tools.

DO NOT guess my preferences, coding standards, or past decisions. Instead, use an agentic loop:
1. SEARCH: Before executing a task, use `search_vault` (semantic search over all my notes, incl. images/PDFs) and `search_memories` (for implicit facts).
2. READ: Use `read_note` to retrieve the full context of any matching guidelines from the vault.
3. GATHER (when external context is needed): pull it read-only via `search_web`, `ingest_media`, or `analyze_github_repo` — then decide what to persist. See the research-loop skill.
4. ACT: Execute the requested task based on the explicitly retrieved context.
5. REMEMBER: Persist explicit knowledge with `write_note` and implicit preferences with `add_memory`. Manage older facts with `search_memories` → `update_memory` / `delete_memory`; `get_memories` samples what is stored (unordered, capped) and `get_memory` fetches one by id.

AI-FIRST VAULT RULES — the server ENFORCES these on write_note/append_to_note. A
rejected write returns the exact reason; fix it and resend. Following them up front
avoids the round-trip:
- FALSE ABSENCE (ANTI-FABRICATION): NEVER assert that a note or fact does not exist without an EXHAUSTIVE search using multiple queries. (Not server-checkable — your responsibility.)
- PREAMBLE (enforced): every note must contain a `## AI Summary` section summarizing context.
- RECENCY MARKERS & SOURCES (enforced): external facts with raw URLs must carry a date/recency marker, e.g., `(as of 2026-06, https://...)`.
- CONFIDENCE LEVELS (enforced): mark inferences or external claims with `(confidence: stated|high|medium|speculation)`.
- NOTE TYPES & PLACEMENT (enforced): `type_meta` must be one of the predefined types — the `write_note` schema lists the current set. The server DERIVES the folder from the type (`person`→`people/`, `decision`→`decisions/`, …) and files the note there, so pass a BARE filename to `write_note`; a folder prefix is ignored and a `vault` that contradicts the type is rejected.
- NAMING AN EXISTING NOTE: because the folder carries the type, two notes of different types may share a basename. `read_note`/`append_to_note`/`rename_note`/`delete_note` take a bare filename normally; where several notes carry it, the server does NOT guess — it returns the candidates as vault-qualified paths (`brain-vault/projects/roadmap.md`, whose folder segment IS the type) and you resend the one you mean. `search_vault` and `list_notes` already name notes in that form, so a hit can be passed straight back.
- WIKILINKS: heavily use Obsidian bidirectional links `[[Note Name]]` for companies, projects, people, concepts. The server auto-links / suggests links to notes that ALREADY exist; creating links for NEW topics is up to you. A title held by several notes is never auto-linked (it would resolve nowhere in particular) — it is named for you to link explicitly.

MEMORY BOUNDARY (details in the memory-capture skill): `write_note` holds CONTENT you
search on demand (people, companies, tech, projects, decisions); `add_memory` holds
BEHAVIOUR that should tune every future session — never content, never ephemeral session
state. A memory is not a fact but a handling rule that follows from one. Memories must
be atomic and start with a behavioural prefix (`[Preference]`/`[Constraint]`/`[Explicit]`/
`[Inferred]`), or the server rejects them.

SKILLS (reusable procedures shared across all my tools):
- A SKILL CATALOG is injected below ("AVAILABLE SKILLS"): each entry is name + when-to-use only, NOT the full body.
- PULL ON DEMAND: When a task matches a skill's when-to-use, call `get_skill` with its name to load the full procedure BEFORE acting. Do not guess a skill's content from its summary.
- AUTHOR/UPDATE: When you learn a reusable, generalizable procedure, persist it with `write_skill` (it is git-backed and shared across every client). Prefer updating an existing skill over creating a near-duplicate.
- RE-READ / RETIRE: The catalog below is a snapshot from the start of this session — after a `write_skill`, `list_skills` returns the current one. A skill that no longer applies is pushed into every future session until you retire it with `delete_skill`.

Loop through these tools until you have sufficient context to proceed.
"""


def load_identity() -> str:
    """Concatenate the prose of all context-vault markdown files into one identity block.

    Bodies only: `date`/`updated`/`type`/`tags`/`ai-first` are how the vault files
    itself, and this block is delivered as authoritative instructions — a reader
    cannot act on a filing marker, so it is context spent on every session start
    for nothing. The `## {name}` heading keeps the files apart in its place.

    Split with vault.split_note rather than the parser: a body opening with a `---`
    rule reads as a frontmatter fence, and the parser takes the text after it too.
    In an index that costs a worse embedding; here it would drop a rule the operator
    wrote, silently. A block that is not valid YAML stays in the output for the same
    reason — the whole file counts as body, so nothing is lost.
    """
    if not config.CONTEXT_DIR.exists():
        return ""
    parts = []
    for f in sorted(config.CONTEXT_DIR.rglob("*.md")):
        # Identity text is fed to the model as authoritative instructions, so a
        # file escaping the vault here is both a disclosure and an injection
        # vector — and it would be read on every session start, unprompted.
        if not is_contained_file(f, config.CONTEXT_DIR):
            continue
        try:
            body = vault.split_note(f.read_text(encoding="utf-8")).body
            parts.append(f"## {f.name}\n{body.lstrip()}")
        except Exception:
            log.exception("Could not read identity file %s", f)
    return "\n\n".join(parts)


# The bootstrap trigger. It is placed FIRST in the static instructions (see
# build_static_instructions) so that if a client truncates the MCP `instructions`
# field to a per-server character budget, the one instruction that matters — call
# get_core_context — survives the cut. Everything else (the full meta prompt, the
# live identity) is delivered by get_core_context itself, so it is safe to lose.
# Clients that ignore the instructions field entirely (e.g. Antigravity) rely on a
# local rule that triggers the same tool call.
CORE_CONTEXT_TRIGGER = (
    "# BOOTSTRAP (do this first)\n"
    "This server holds my live identity, my authoritative rules, my skill catalog, "
    "and an agentic search-before-acting workflow. As your FIRST action in a new "
    "session, call the `get_core_context` tool — it returns that full, current context "
    "in one call. If your client defers or lazily loads tool schemas (the tool is listed "
    "by name only, not yet callable), resolve/load it first via that client's own "
    "tool-discovery mechanism, THEN call it — do not skip the call just because it is not "
    "immediately invocable. Do NOT act on the task before you have. Call it AGAIN whenever "
    "the conversation is compacted/summarized or you no longer have my identity and rules "
    "clearly in view: re-calling is cheap and always returns the current context, so "
    "when in doubt re-call rather than guess.\n"
)


def build_static_instructions() -> str:
    """Content for the server's MCP `instructions` field (frozen at startup).

    Nothing but the get_core_context bootstrap trigger. Clients truncate this
    field to a per-server character budget (Claude Code among them), so keeping
    it to the single load-bearing line means there is nothing left to clip. The
    full agentic meta prompt, the skill catalog, and the live-editable identity
    are all delivered by get_core_context, fresh, in one call — so they need not
    ride on this field, and a vault edit is never stale until a restart.
    """
    return CORE_CONTEXT_TRIGGER


def build_prompt() -> str:
    """The agentic meta prompt with identity context and skill catalog injected.

    The full, live context: meta rules + live identity + live catalog. Backs both
    the get_core_context tool and the agentic_second_brain MCP prompt.
    """
    full_prompt = AGENTIC_META_PROMPT
    identity = load_identity()
    if identity:
        full_prompt += f"\n\n# WHO I AM & MY RULES (always authoritative)\n{identity}"
    catalog = skill_catalog_text()
    if catalog:
        full_prompt += (
            "\n\n# AVAILABLE SKILLS (catalog only — call get_skill to load a body)\n" + catalog
        )
    return full_prompt


# --- SKILLS LAYER -------------------------------------------------------
# A skill is a markdown file with frontmatter:
#   ---
#   name: review-pr
#   description: How I review pull requests
#   when_to_use: Reviewing a PR or diff for correctness and style
#   version: 1
#   ---
#   <full procedure body>
# The catalog (name + when_to_use) is pushed into the prompt; the body is pulled
# on demand via get_skill. Skills live in SKILLS_DIR and are git-persisted.

# Cap for the fields that make up a catalog line. Generous next to the shipped seeds
# (longest when_to_use: 195 chars) — this bounds a single entry, it is not a style rule.
_MAX_CATALOG_FIELD_CHARS = 300

def _skill_name_to_path(name: str):
    """Resolve a skill name to a safe .md path inside SKILLS_DIR (blocks traversal)."""
    filename = name if name.endswith(".md") else f"{name}.md"
    return validate_safe_path(config.SKILLS_DIR, filename)


def _parse_skill(path):
    """Return frontmatter metadata + body for a skill file, with name fallbacks."""
    post = frontmatter.load(path)
    meta = post.metadata
    return {
        "name": meta.get("name", path.stem),
        "description": meta.get("description", ""),
        "when_to_use": meta.get("when_to_use", meta.get("description", "")),
        "version": meta.get("version", 1),
        "body": post.content,
    }


# Frontmatter key that marks a file as a MANAGED SEED and records the hash of the
# CANONICAL seed content it was installed from. A pristine managed seed re-hashes
# to this value → safe to update. Any edit changes the hash → the file is treated
# as user-owned and never overwritten. A skill with no marker is a user-authored
# file of the same name → only adopted if provably identical to a shipped seed.
SEED_HASH_KEY = "seed_hash"


def _seed_body_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(post) -> str:
    """Serialise a skill to its marker-free canonical form. Comparing/hashing this
    form (rather than raw file bytes) makes the check immune to how frontmatter
    re-serialises YAML — the only thing that matters is content + non-marker meta."""
    meta = dict(post.metadata)
    meta.pop(SEED_HASH_KEY, None)
    return frontmatter.dumps(frontmatter.Post(post.content, **meta))


def _canonical_of_text(text: str) -> str:
    return _canonical(frontmatter.loads(text))


def _canonical_versionless(post) -> str:
    """The canonical form with `version` stripped as well — see _adoptable."""
    meta = dict(post.metadata)
    meta.pop("version", None)
    return _canonical(frontmatter.Post(post.content, **meta))


def _adoptable(existing, shipped_text: str) -> bool:
    """Whether an unmarked file is provably the seed as shipped, and so safe to adopt.

    `version` is the seed author's field, bumped in the repo and never by the user, so
    a file that differs in nothing but the version is still untouched and must not be
    locked out of management by it. Everything else — the body, `description`,
    `when_to_use` — has to match exactly, because those the user may have edited.
    """
    return _canonical_versionless(existing) == _canonical_versionless(frontmatter.loads(shipped_text))


def _stamp_seed(shipped_text: str) -> str:
    """Return the shipped seed with the managed-seed marker injected — the marker is
    the hash of the seed's canonical form, so a pristine copy is later recognisable."""
    post = frontmatter.loads(shipped_text)
    post[SEED_HASH_KEY] = _seed_body_hash(_canonical(post))
    return frontmatter.dumps(post)


def seed_skills() -> None:
    """Install / update repo-shipped seed skills in SKILLS_DIR — managed seeds.

    The server ships default-on procedures (e.g. session-capture) and keeps them
    current WITHOUT clobbering the user's own edits. Per file, on startup:
      - missing        → install the shipped seed (stamped with a seed marker).
      - no seed marker  → adopted into management when it is provably the shipped seed
        (ignoring `version`, which only the seed author bumps); otherwise it counts as
        a user-authored file of the same name and is NEVER touched.
      - marker matches the file's current body → pristine seed → update in place
        when the shipped version differs.
      - marker present but body diverged → the user edited it → leave it, and log
        that a newer seed is available.

    Resilient per file: one bad seed never blocks the others or startup.
    """
    if not config.SEED_SKILLS_DIR.exists():
        return
    config.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    for src in sorted(config.SEED_SKILLS_DIR.glob("*.md")):
        try:
            _seed_one(src)
        except Exception:
            log.exception("Could not seed skill %s", src)


def _seed_one(src) -> None:
    dest = config.SKILLS_DIR / src.name
    shipped_text = src.read_text(encoding="utf-8")
    shipped_hash = _seed_body_hash(_canonical_of_text(shipped_text))

    if not dest.exists():
        dest.write_text(_stamp_seed(shipped_text), encoding="utf-8")
        log.info("Seeded skill %s", src.name)
        return

    existing = frontmatter.load(dest)
    marker = existing.metadata.get(SEED_HASH_KEY)

    if marker is None:
        # A file with no marker is hands-off by default: it may be a genuinely
        # user-authored skill of the same name. To keep the upgrade automatic
        # without ever clobbering real user content, we ADOPT it only when it is
        # provably the seed as shipped — then stamp it so it's managed from now on.
        if _adoptable(existing, shipped_text):
            dest.write_text(_stamp_seed(shipped_text), encoding="utf-8")
            log.info("Adopted pristine unmarked seed %s into management", src.name)
        else:
            log.info("Skill %s has no seed marker — treating as user-owned, not touched", src.name)
        return

    if _seed_body_hash(_canonical(existing)) == marker:
        # Managed and pristine (marker matches the file's own current content).
        if marker != shipped_hash:
            dest.write_text(_stamp_seed(shipped_text), encoding="utf-8")
            log.info("Updated seed %s to the shipped version", src.name)
        # else: already on the shipped version, nothing to do.
    else:
        # Marker present but the content no longer matches it → the user edited a
        # managed seed. Never clobber; just note that a newer version ships.
        if marker != shipped_hash:
            log.info("Seed %s was edited by the user — keeping it; a newer version ships in the repo",
                     src.name)


def list_skill_meta() -> list:
    """Return catalog metadata (no body) for every skill file."""
    return _scan_skills()[0]


def unreadable_skills() -> list[str]:
    """Filenames of skills that exist but cannot be parsed."""
    return _scan_skills()[1]


def _scan_skills() -> tuple[list, list[str]]:
    """Parse every skill file. Returns (catalog metadata, names that failed to parse)."""
    if not config.SKILLS_DIR.exists():
        return [], []
    out, broken = [], []
    for f in sorted(config.SKILLS_DIR.rglob("*.md")):
        # The catalog goes into every get_core_context call, and _parse_skill reads
        # the file — so a symlink escaping the skills dir would be both a disclosure
        # and an injection vector, unprompted on every session start.
        if not is_contained_file(f, config.SKILLS_DIR):
            continue
        try:
            s = _parse_skill(f)
            out.append({k: s[k] for k in ("name", "description", "when_to_use", "version")})
        except Exception:
            log.exception("Could not parse skill %s", f)
            broken.append(f.name)
    return out, broken


def skill_catalog_text() -> str:
    """One line per skill: name — when_to_use. This is what gets pushed into the prompt."""
    return "\n".join(
        f"- {s['name']} — {s['when_to_use']}" for s in list_skill_meta()
    )


@register(
    "list_skills", "List the catalog of available skills (name + when_to_use, no bodies).",
    {"type": "object", "properties": {}},
)
async def list_skills_tool(arguments: dict) -> list:
    meta, broken = await asyncio.to_thread(_scan_skills)
    # A skill whose frontmatter does not parse has no catalog line to render, so the
    # listing is the only place its absence can be noticed at all — silence here reads
    # as "that skill was never written".
    note = ("\n\nUnreadable (frontmatter is not valid YAML, fix or rewrite with "
            "write_skill): " + ", ".join(broken)) if broken else ""
    if not meta:
        return text("No skills defined yet." + note)
    lines = [f"- {s['name']} (v{s['version']}) — {s['when_to_use']}" for s in meta]
    return text("Available skills:\n" + "\n".join(lines) + note)


@register(
    "get_core_context",
    "Load the full core context — identity, AI-first rules, and skill catalog — in "
    "ONE call. Call this FIRST in every new session, before acting on the user's task.",
    {"type": "object", "properties": {}},
)
async def get_core_context_tool(arguments: dict) -> list:
    return text(await asyncio.to_thread(build_prompt))


@register(
    "get_skill", "Load the full body of a skill by name (pull on demand).",
    {"type": "object", "properties": {"name": {"type": "string", "description":
        "Skill name as listed in the catalog or by list_skills."}}, "required": ["name"]},
)
async def get_skill_tool(arguments: dict) -> list:
    try:
        path = _skill_name_to_path(arguments["name"])
    except ValueError as e:
        return text(str(e), OUTCOME_REJECTED)
    if not path.exists():
        return text(f"Skill '{arguments['name']}' not found. Use list_skills to see the catalog.",
                    OUTCOME_REJECTED)
    try:
        skill = await asyncio.to_thread(_parse_skill, path)
    except Exception as e:
        # Skills are files a user may hand-edit, so a malformed frontmatter block is a
        # user error, not a server fault. list_skill_meta already skips such a file (it
        # is missing from the catalog for the same reason); asked for by name it has to
        # say so, rather than surfacing a parser traceback with no filename in it.
        log.exception("Could not parse skill %s", path)
        return text(f"Skill '{arguments['name']}' cannot be read — its frontmatter is not "
                    f"valid YAML: {' '.join(str(e).split())}\n\nFix the block in "
                    f"{path.name} (quote a value containing ': ', use spaces instead of "
                    f"tabs), or rewrite the skill with write_skill.", OUTCOME_REJECTED)
    header = f"# Skill: {skill['name']} (v{skill['version']})\nWhen to use: {skill['when_to_use']}\n\n"
    return text(header + skill["body"])


@register(
    "write_skill", "Create or update a reusable skill (git-persisted, shared across clients).",
    {"type": "object", "properties": {
        "name": {"type": "string", "description":
                 "Short kebab-case name, e.g. 'release-checklist'. An existing skill of this "
                 "name is overwritten."},
        "description": {"type": "string", "description":
                        "One line saying what the procedure does. Rendered in the skill catalog."},
        "when_to_use": {"type": "string", "description":
                        "One line naming the situations that should trigger this skill. This is "
                        "all a future session sees before deciding to load the body, so describe "
                        "the trigger, not the content."},
        "body": {"type": "string", "description":
                 "The procedure itself, in Markdown — at least 20 characters. Only loaded when "
                 "get_skill is called, so length here costs nothing per session."}},
     "required": ["name", "description", "when_to_use", "body"]},
)
async def write_skill_tool(arguments: dict) -> list:
    config.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    # Schema-level validation for skills (the structural invariant).
    for fld in ("name", "description", "when_to_use", "body"):
        if not str(arguments.get(fld, "")).strip():
            return text(f"Skill rejected: '{fld}' must not be empty.", OUTCOME_REJECTED)
    if len(arguments["body"].strip()) < 20:
        return text("Skill rejected: body is too short to be a useful procedure (min 20 chars).",
                    OUTCOME_REJECTED)
    # The catalog fields go into the INSTRUCTION channel: get_core_context renders one
    # `- <name> — <when_to_use>` line per skill on every session start. A newline in
    # one of them forges further catalog lines (or arbitrary instructions) in a
    # position the model reads as the server's own words, so they must stay
    # single-line. The cap keeps one entry from crowding out the rest of the catalog —
    # the longest shipped seed uses 195 characters.
    for fld in ("name", "description", "when_to_use"):
        value = str(arguments[fld])
        if "\n" in value or "\r" in value:
            return text(f"Skill rejected: '{fld}' must be a single line — it is rendered as one "
                        f"line of the skill catalog. Put the detail in the body instead.", OUTCOME_REJECTED)
        if len(value) > _MAX_CATALOG_FIELD_CHARS:
            return text(f"Skill rejected: '{fld}' is {len(value)} characters, "
                        f"max {_MAX_CATALOG_FIELD_CHARS}. Keep the catalog line short and "
                        f"put the detail in the body.", OUTCOME_REJECTED)
    try:
        path = _skill_name_to_path(arguments["name"])
    except ValueError as e:
        return text(str(e), OUTCOME_REJECTED)
    body_bytes = len(arguments["body"].encode("utf-8"))
    if body_bytes > config.MAX_FILE_SIZE_BYTES:
        return text(f"Skill body is {body_bytes} bytes, over the {config.MAX_FILE_SIZE_BYTES} "
                    f"byte limit (MAX_FILE_SIZE_BYTES). Shorten the procedure, or split it "
                    f"across two skills.", OUTCOME_REJECTED)
    # Preserve/increment version when updating an existing skill.
    version = 1
    if path.exists():
        try:
            version = int((await asyncio.to_thread(_parse_skill, path))["version"]) + 1
        except Exception:
            version = 1
    post = frontmatter.Post(
        arguments["body"],
        name=arguments["name"],
        description=arguments["description"],
        when_to_use=arguments["when_to_use"],
        version=version,
    )
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    await enqueue_sync(f"MCP Bot: Wrote skill {path.stem} v{version}")
    return text(f"Saved skill '{arguments['name']}' (v{version}).")


@register(
    "delete_skill",
    "Retire a skill: delete it from the catalog (git-persisted, shared across clients). "
    "A skill the repo still ships is refused — seeding would reinstall it on the next start.",
    {"type": "object", "properties": {
        "name": {"type": "string", "description": "Skill name as listed by list_skills."}},
     "required": ["name"]},
)
async def delete_skill_tool(arguments: dict) -> list:
    name = arguments["name"]
    try:
        path = _skill_name_to_path(name)
    except ValueError as e:
        return text(str(e), OUTCOME_REJECTED)
    # A skill the repo still ships cannot be retired from a client: seed_skills()
    # reinstalls anything missing from SKILLS_DIR on the next start, so the delete
    # would report success and then silently undo itself. Decided by whether the file
    # is in SEED_SKILLS_DIR — mechanically, per ADR 5, not by reading the skill. A
    # seed the repo has DROPPED is therefore deletable, which is the case that needs
    # this tool: nothing else removes a managed seed left behind by a rename.
    if (config.SEED_SKILLS_DIR / path.name).exists():
        return text(f"Skill rejected: '{name}' is a seed shipped with the server and would be "
                    f"reinstalled on the next start. Edit it with write_skill instead — an "
                    f"edited seed is never overwritten.", OUTCOME_REJECTED)
    if not path.exists():
        return text(f"Skill '{name}' not found. Use list_skills to see the catalog.", OUTCOME_REJECTED)
    # No deindex step: skills are not in the semantic index — reindex_vault covers the
    # brain, context and media directories, not SKILLS_DIR. They reach the model through
    # the catalog in get_core_context, which reads the files directly.
    path.unlink()
    await enqueue_sync(f"MCP Bot: Deleted skill {path.stem}")
    return text(f"Deleted skill '{name}'.")
