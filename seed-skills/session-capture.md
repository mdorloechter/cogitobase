---
name: session-capture
description: Default-on discipline for capturing what a session produced before it ends.
when_to_use: "At the end of any session that pushed a commit, opened/merged a PR, touched an issue tracker or documentation, or made a non-trivial architectural choice — before ending, capture what is missing."
version: 3
---
# Session capture discipline

Capturing is default-on, not opt-in. Before you end a working session, ask: did anything
happen here that a future Claude would need and could not reconstruct from the code alone?
If yes, persist it now — the cost of capturing is small, the cost of losing it is high.

Two things drive where a capture goes:
- `capture_session_retro` — an end-of-session retro per project. Records what Shipped,
  what Worked, and where the Friction was. Creates the project note, or appends a dated
  entry at the end of its `## Retros` section — so the entries stay in one chronological
  run no matter what sections the note has grown below it. Use this tool — it owns the
  create-vs-append logic. If it reports that it had to re-create `## Retros`, the heading
  was renamed or removed: check whether older entries are sitting under another one.
- Everything else is a normal `write_note` into the right folder, following the body
  scaffold in the `zettelkasten-discipline` skill:
  - a **decision** (including architecture/design choices) → `decisions/` as `type: decision`
    (Context / Options / Decision / Consequences).
  - a reusable **learning** (a gotcha, a pattern, a fix that generalizes) → `learnings/`
    as `type: learning` (Situation / Learning / Application).

Trigger checklist — if any of these happened, capture before ending:
- a commit was pushed or a PR was opened/merged,
- a ticket, issue tracker, or documentation page was touched,
- a meaningful design/architecture decision was made,
- you learned something you would not want to relearn.
