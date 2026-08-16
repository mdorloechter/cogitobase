---
name: zettelkasten-discipline
description: Zettelkasten strategy for knowledge capture, meetings, and research
when_to_use: Whenever you are asked to save notes about a meeting, a conversation, a research topic, a new technology, or any unstructured information.
version: 3
---

## Zettelkasten Strategy for Knowledge Capture

When the user asks you to save a note about unstructured or multi-faceted information, never dump everything into a single massive file. Instead, strictly apply the following isolating and linking strategy.

### One axis: `type` IS the folder

The note `type` and its folder are the SAME decision — there is no separate "genre" type. A note about a person is `type: person` in `people/`; a note about a technology is `type: tech` in `tech/`. Genres like a *retro*, a *devlog entry*, an *ADR*, or a *meeting log* are **body sections** inside one of these notes, never their own type.

> **The server ENFORCES this.** You pass a bare `filename` and a `type_meta`; the server derives the folder from the type and files the note there — a folder prefix in the `filename` is ignored, and the `vault` must match the type (`identity`/`standard` → context-vault, everything else → brain-vault) or the write is rejected. So you cannot misfile a note: pick the right `type_meta` and the placement follows automatically.

| `type` | Folder | What goes here |
|--------|--------|----------------|
| `person` | `people/` | One atomic note per person |
| `company` | `companies/` | One atomic note per company/organization |
| `tech` | `tech/` | One note per technology / tool / library |
| `concept` | `concepts/` | One note per abstract concept or idea |
| `project` | `projects/` | Project notes, meeting narratives, devlog entries, session retros |
| `research` | `research/` | Research / investigation logs |
| `decision` | `decisions/` | Architecture/design decisions (ADRs) |
| `learning` | `learnings/` | Reusable learnings |
| `daily` | `daily/` | The chronological backbone: `daily/YYYY-MM-DD.md` |
| `inbox` | `inbox/` | Holding area for fast, not-yet-sorted captures (also written by `capture_inbox`) |
| `identity` | *context-vault* | Who the user is (lives in the context-vault, not brain) |
| `standard` | *context-vault* | Coding/working standards (lives in the context-vault) |

> Use these EXACT names — never invent a synonym (`technologies/` for `tech/`, `meetings/` for `projects/`), or the graph fragments. Tasks are NOT a type: track them as `- [ ]` checkboxes inside a `project` or `daily` note. Media (images/PDFs) live outside `brain-vault/` in the `media/` directory, linked with `![alt](../media/file)` / `[PDF](../media/file)`.

> The `projects/` and `inbox/` folders are also written by the capture tools (`capture_session_retro`, `capture_inbox`) — hand-written and captured notes land in the SAME place. `capture_session_retro` appends dated entries at the end of the project note's `## Retros` section, so your own sections can live below it without the entries landing in them. `append_to_note` takes the same `section` argument for hand-written appends — use it, or the text goes to the end of the file and thereby under whichever section is last.

> **When to use `inbox/`:** only when you genuinely cannot yet place a note (incomplete info, mid-flow capture). Prefer filing directly into the right folder. Treat `inbox/` as temporary — when a note becomes clear, **graduate** it into `people/`, `tech/`, `projects/`, … and update any links. An inbox that never drains defeats the point.

### Frontmatter

The server owns `date` (birth), `updated` (last write), `type`, `tags`, `ai-first` — you never write these by hand. Two optional lifecycle fields are available on `write_note` when a note evolves or gets replaced:

- `status`: `active` | `paused` | `archived` | `superseded` — mostly for `project`, `decision`, `learning`.
- `supersedes`: a `[[wikilink]]` to the note this one replaces.

### Body scaffolds per type

Every note keeps a single `# H1` and a `## AI Summary` (both enforced by the server). The sections below are guidance, not rules — adapt when a note doesn't fit. Prefer `[[wikilinks]]` in the connective sections so the graph grows. Add `(as of YYYY-MM, https://…)` next to external sources and `(confidence: …)` to speculative claims.

**`person`**
```markdown
# <Name>

## AI Summary
<Who this is, in one sentence — role + why relevant.>

## Role & Context
- Organization: [[<Company>]]
- Role: <title / function>
- How we're connected: <context>

## Key Facts
- <standing facts: expertise, preferences, history>

## Connections
- Works with [[<Person>]], on [[<Project>]], uses [[<Tech>]]
```

**`company`**
```markdown
# <Company / Org>

## AI Summary
<What they do + my relationship (customer/employer/vendor).>

## What they do
<1–2 sentences.>

## Relationship
- My connection: <context>
- Key people: [[<Person>]], [[<Person>]]

## Notes
- <standing facts, contracts, history>
```

**`tech`**
```markdown
# <Technology / Tool>

## AI Summary
<What it is + my relationship to it (use it / evaluating / rejected).>

## What it is
<1–2 neutral sentences.>

## My Take
- Where I use it: [[<Project>]]
- Assessment: <pro/contra> (confidence: …)

## References
- Docs / links (as of YYYY-MM, https://…)
```

**`concept`**
```markdown
# <Concept>

## AI Summary
<The idea in one sentence.>

## Definition
<What the term means — precisely.>

## Why it matters / Application
<Where it applies for me, how I use it.>

## Related
- [[<Concept>]], [[<Tech>]]
```

**`project`** — the general project note (meetings, devlog entries, and retros all live here as sections). Pass `status: active` as a `write_note` parameter (NOT in `content` — the server writes the frontmatter):
```markdown
# <Project>

## AI Summary
<Goal of the project + where it stands, in one sentence.>

## Current State
<Where are we? Last milestone.>

## Open Points
- [ ] <open item>

## Retros
### Retro YYYY-MM-DD   ← appended by capture_session_retro
```

**`research`**
```markdown
# <Research Topic>

## AI Summary
<Question/goal of the research + key finding.>

## Question
<What did I want to find out?>

## Findings
- <finding> (as of YYYY-MM, https://…, confidence: …)

## Open Questions / Next
- [ ] <open item>
```

**`decision`** — an ADR (architecture too — architecture decisions are just decisions). Pass `status`/`supersedes` as `write_note` parameters when the decision is later revised or replaced:
```markdown
# <Decision>

## AI Summary
<What was decided + the core reason.>

## Context
<Problem, constraints.>

## Options
<Options considered.>

## Decision
<The chosen path and its reasoning.>

## Consequences
<What follows, risks.>
```

**`learning`**
```markdown
# <Learning>

## AI Summary
<The reusable insight in one sentence.>

## Situation
<Where it came up.>

## Learning
<What generalizes.>

## Application
<How to apply it next time.>
```

**`daily`** — the chronological backbone (kept light):
```markdown
# YYYY-MM-DD

## AI Summary
<One-sentence summary of the day.>

## Log
- [[<context-note>]]: <what happened, with [[Entities]]>

## Tasks
- [ ] <open>
```

**`standard`** (context-vault):
```markdown
# <Standard: Topic>

## AI Summary
<The rule in one sentence.>

## Rule
<What applies — imperative.>

## Rationale
<Why.>

## Examples
<good / bad>
```

### Procedure

1. **Isolate Entities (People, Companies, Concepts, Technologies):**
   - For every distinct entity mentioned, create a separate atomic note in its folder (`people/`, `companies/`, `tech/`, `concepts/`).
   - The entity note holds only facts, context, and standing information about that specific entity.

2. **The Main Context Note (Meeting, Project, or Research log):**
   - Save the main narrative in the matching folder: `projects/` for project work and meeting narratives (temporal events), `research/` for investigations.
   - Link the entities directly in the text: e.g. `[[Jane Doe]] from [[Acme Corp]] uses [[React]]`.
   - Record only the actual discussion points, decisions, insights, and ToDos here.

3. **The Chronological Backbone (Daily Notes):**
   - After creating or updating the entities and the main context note, always create or append to `daily/YYYY-MM-DD.md`.
   - Add a brief bullet linking to the new context note and mentioning the entities.
   - Example: `- [[2026-07-02-Acme-Corp-React]]: Met with [[Jane Doe]] to discuss [[React]].`

4. **Execution Procedure (CRITICAL):**
   - First, use `search_vault` to check if the entity already exists.
   - If it exists, update it (`read_note` → `write_note` with `overwrite=true`, or `append_to_note`).
   - If it does not exist, create it via `write_note`.
   - Only create the main narrative note *after* the entities exist, so all link targets resolve and the graph stays sound.
