---
name: memory-capture
description: Discipline for capturing implicit preferences and behavioral facts in Mem0 (not the Vault).
when_to_use: "When the user states a preference, corrects your behavior, or you learn a recurring fact about their tech stack, team, or workflow that should tune future sessions."
version: 6
---

# Implicit Memory Capture (Mem0)

Mem0 is permanent, cross-session memory that tunes **your behaviour** to the user.
It holds DISPOSITIONS, not CONTENT.

## The one distinction that decides everything: INJECTED vs. SEARCHED

- **Mem0 = injected.** A standing rule about how you should *act*, meant to shape
  every future session even when nobody searches for it. It changes your defaults.
- **Vault (`write_note`) = searched.** Content about the world — a person, a company,
  a technology, a project, a decision. You pull it with `search_vault` when a task
  needs it. It is reference material, not a behavioural rule.

A Mem0 memory is never a *fact*; it is a *handling instruction that follows from one*.
"Acme runs on PostgreSQL" is content → a `company`/`tech` Vault note. "For Acme code,
default to PostgreSQL, never Mongo" is behaviour → Mem0.

## Decide in three questions
1. Does it describe the world / a project / a person / a decision? → **Vault**
   (`write_note`, pick the folder — see the zettelkasten-discipline skill).
2. Is it a standing rule for *my behaviour*, true across sessions? → **Mem0**.
3. Is it just current session state or a temporary thought? → **store nothing**.

**Granularity tiebreaker:** if it wants a heading, a section, a `[[link]]`, or a date,
it is a Note. One permanent, structureless sentence → Mem0.

## Rules the server ENFORCES (a violation is rejected)
1. **Atomic:** exactly one rule per memory — no paragraphs.
2. **Behavioural category prefix (required):** the memory MUST START with one of these
   exact prefixes, or the write is rejected:
   - `[Preference]` — a soft preference ("I prefer dark UIs").
   - `[Constraint]` — a hard rule to always respect ("never use Tailwind").
   - `[Explicit]` — the user stated it directly.
   - `[Inferred]` — you deduced it from context.
   There is intentionally NO `[Tech]` or `[Project]` prefix: a tech/project *fact* is a
   Vault note, not a memory. To scope a preference to a project, say so in the text —
   `[Constraint] In the Acme project, never use Mongo`.
   Memories that lack a valid prefix or exceed the length limit are rejected — fix and resend.

## The workflow (CRITICAL — update, don't clutter)
1. **Search first:** ALWAYS call `search_memories` with relevant keywords before saving.
2. **Update over duplicate:** if a similar/outdated fact exists, note its `memory_id`
   and call `update_memory` — Mem0 must never hold contradictory facts. (`add_memory`
   also reports similar existing IDs to help you decide.)
3. **Create only if new:** if `search_memories` returns nothing relevant, call `add_memory`.
   It stores your wording verbatim, prefix included, and returns the new `memory_id` —
   keep it if you may need to correct the fact later in the session. Resending a fact
   the store already holds word for word is refused and names the existing ID: that is
   an `update_memory` (or `delete_memory`), not a retry.

## Reviewing what is stored
`search_memories` answers "is this already known?". To see the store itself, call
`get_memories` — it returns up to 50 with their IDs, unordered, with no paging behind it.
Past 50 that is a SAMPLE, not the store: do not conclude from it that a rule is absent,
and do not present it to the user as everything you remember. A specific fact is reached
by searching. `get_memory` fetches a single one by ID, for when a search hit or an
`add_memory` reply named an ID you want in full.

Read the list when the user asks what you remember about them (say how many it holds and
that it is capped, rather than implying it is all of them), and when a session keeps
hitting a rule that seems wrong: a memory tunes every future session silently, so an
outdated one is only ever found by looking. Fix it with `update_memory`, or retire it
with `delete_memory`. Unlike a Vault note, a memory has no history to recover it from.
