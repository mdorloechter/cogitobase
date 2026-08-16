"""Capture tools — structured, low-friction entry points for the OKF note types.

These tools take a handful of structured fields, BUILD an OKF-conformant note body
(with the required `## AI Summary` block), and then DELEGATE to the existing
vault pipeline (vault.write_note / vault.append_to_note). They deliberately do NOT
re-implement enforcement — the validating server still owns it.
"""
import re
from datetime import datetime

import config
import vault
from registry import register, text, OUTCOME_REJECTED
from security import vault_qualified_path


def _slugify(text: str) -> str:
    """Lowercase, non-alphanumeric -> '-', collapse repeats, strip leading/trailing '-'."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower())
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")


# How much of a note stands in for a missing summary. Long enough to identify the note
# in a folder listing, short enough that it cannot pass for a full one.
_EXCERPT_CHARS = 200


def _excerpt(body: str) -> str:
    """The opening of `body`, with an ellipsis when there is more of it."""
    return body if len(body) <= _EXCERPT_CHARS else body[:_EXCERPT_CHARS].rstrip() + "…"


@register(
    "capture_inbox",
    "Quickly capture an unsorted note into the inbox/ holding area. Use when you want "
    "to record something fast without deciding its final folder yet — graduate it into "
    "people/tech/projects/etc. later. Only a free-text note is required — pass a "
    "`summary` too, or the note says it has none.",
    {"type": "object", "properties": {
        "note": {"type": "string", "description": "The text to capture, in any shape — an inbox "
                                                 "note carries no structural requirements."},
        "title": {"type": "string", "description": "Optional short title; derived from the note if omitted."},
        "summary": {"type": "string", "description": "Optional one-line summary of the note. "
                    "Omit it and the note carries an excerpt, labelled as one."}},
     "required": ["note"]},
)
async def capture_inbox(arguments: dict) -> list:
    body = arguments["note"].strip()
    if not body:
        return text("'note' is empty. Pass the text to capture.", OUTCOME_REJECTED)
    # Title is optional — the whole point of an inbox is low friction. Derive a
    # short one from the note's first line when the caller doesn't supply it.
    title = (arguments.get("title") or "").strip() or body.splitlines()[0][:60].strip()
    date = datetime.now().strftime("%Y-%m-%d")
    slug = _slugify(title) or "note"
    # The preamble ADR 5 enforces is the caller's to write — summarising is semantic
    # work, and the server does not do semantics. Without one the note carries the
    # note's opening instead, labelled as an excerpt: a reader who later meets this
    # note has to be able to tell a summary from a machine-made stand-in, and an
    # unmarked first-200-characters reads exactly like the real thing.
    summary = (arguments.get("summary") or "").strip()
    preamble = summary or f"[No summary supplied — excerpt follows]\n{_excerpt(body)}"
    content = f"# {title}\n\n## AI Summary\n{preamble}\n"
    # A note that fits entirely in the excerpt is already printed above; repeating it
    # under ## Note would store the same text twice in one file.
    if summary or len(body) > _EXCERPT_CHARS:
        content += f"\n## Note\n{body}\n"
    # Folder is derived from type_meta="inbox" (→ inbox/) by write_note; we pass a
    # bare filename and never hardcode the folder prefix.
    return await vault.write_note({
        "vault": "brain",
        "filename": f"{date}-{slug}.md",
        "title": title,
        "type_meta": "inbox",
        "tags": ["inbox"],
        "content": content,
    })


# The heading retro entries live under. One constant, because the note's own summary
# names it and the append targets it — two literals would drift into a note that
# describes a section its entries are not in.
_RETRO_SECTION = "## Retros"


@register(
    "capture_session_retro",
    "Capture an end-of-session retro into a per-project note (creates the note, or "
    "appends a dated entry at the end of its '## Retros' section).",
    {"type": "object", "properties": {
        "project": {"type": "string", "description":
                    "Project name. It selects the note the entry is filed in, so reuse the exact "
                    "wording of earlier retros for the same project."},
        "shipped": {"type": "string", "description": "What actually got done this session."},
        "worked": {"type": "string", "description":
                   "What worked well enough to repeat — the part worth carrying into the next session."},
        "friction": {"type": "string", "description":
                     "What slowed the session down, so the next one can avoid it."}},
     "required": ["project", "shipped", "worked", "friction"]},
)
async def capture_session_retro(arguments: dict) -> list:
    project = arguments["project"].strip()
    if not project:
        return text("'project' is empty. Pass the project name the retro belongs to.",
                    OUTCOME_REJECTED)
    date = datetime.now().strftime("%Y-%m-%d")
    slug = _slugify(project)
    # Folder is derived from type_meta="project" (→ projects/) by write_note, so
    # we pass a bare filename there.
    filename = f"{slug}.md"
    retro = (
        f"### Retro {date}\n"
        f"- Shipped: {arguments['shipped']}\n"
        f"- Worked: {arguments['worked']}\n"
        f"- Friction: {arguments['friction']}\n"
    )
    # Create-vs-append: a project retro note accumulates dated entries.
    target = config.BRAIN_DIR / config.TYPE_FOLDER["project"] / filename
    if target.exists():
        # Append to the note we just checked, by its vault-qualified path. A bare
        # basename would be refused as ambiguous whenever another type holds a
        # same-named note (concepts/<slug>.md), even though the target here is
        # never in doubt — it is derived, not client-supplied.
        #
        # Named section, not the file end: the note itself says its entries live under
        # '## Retros', and a project note is exactly the kind that grows sections of the
        # author's own below it. Filing there keeps that claim true, and keeps the
        # entries in one chronological run.
        return await vault.append_to_note({
            "filename": vault_qualified_path(target),
            "content": retro,
            "section": _RETRO_SECTION,
        })
    content = (
        f"# {project}\n\n"
        f"## AI Summary\n"
        f"Project retro log for {project}. Retro entries under '{_RETRO_SECTION}', "
        f"appended in chronological order (oldest first).\n\n"
        f"{_RETRO_SECTION}\n{retro}"
    )
    return await vault.write_note({
        "vault": "brain",
        "filename": filename,
        "title": project,
        "type_meta": "project",
        "tags": ["project"],
        "content": content,
    })
