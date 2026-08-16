---
name: research-loop
description: Loop for gathering external context (web, articles, YouTube, GitHub repos) read-only, then persisting it as sourced Vault notes.
when_to_use: "When a task needs information that is not already in the Vault or Mem0 — current facts from the web, the content of an article or YouTube video, or the structure of a public GitHub repo."
version: 4
---

# Research Loop

The augmentation tools are **read-only gatherers**. They fetch external context but
persist NOTHING on their own — you decide afterwards what is worth a note. This keeps
the server "dumb" and every fetch goes through an SSRF-safe path.

## The tools
- `search_web` — one round of web search: titles, URLs and a short extract each
  (`max_results`, default 3). It does NOT follow links or refine the query, so treat
  it as discovery: when a result looks like the answer, fetch that URL for the full
  text — with `ingest_media`, or with your own fetch tool if you have one.
- `ingest_media` — pull the transcript of a YouTube URL or the readable text of an article
  URL. Long sources are cut, and the header says so — `(first 50000 of 214883 chars)`
  means you are holding a fragment. Cite only what you actually read.
- `analyze_github_repo` — the file tree and root README of a PUBLIC **github.com** repo.
  A file list is not an architecture: read it as a map of where to look, then fetch the
  files that matter. Watch the header — `(300 of 4213 files)` or a `truncated` note means
  you are holding part of the repo, not all of it.

## The loop
1. **Check the Vault first.** Run `search_vault` (and `search_memories`) before going
   external — never re-research what you already know. Do NOT claim something is absent
   without an exhaustive search (multiple queries). Read the `Score` on each hit: search
   always returns the nearest notes it has, so low scores across several distinct queries
   are your evidence that the Vault holds nothing — a hit is not an answer because it was
   returned. Widen with `limit` (max 20) when you need to see how far the field drops off.
2. **Gather** with the matching tool above. Expect raw, unverified text back. A result
   whose extract reads `[none extracted]`, `[skipped — HTTP …]` or `[no transcript
   available]` was NOT read — say so, or fetch that URL yourself; never present a
   snippet as if you had read the page.
3. **Judge & distill.** Extract only the durable, relevant facts. Discard boilerplate.
4. **Persist with `write_note`**, obeying the enforced Vault rules — this is where most
   augmentation writes get rejected if you skip a marker:
   - Include a `## AI Summary` section.
   - Every external fact with a raw URL needs a recency marker: `(as of 2026-07, https://…)`.
   - Tag confidence on inferences/claims: `(confidence: stated|high|medium|speculation)`.
   - Pick a fitting `type_meta` (often `research`), and wikilink entities you mention
     (`[[Acme Corp]]`, `[[React]]`) — see the zettelkasten-discipline skill for structure.
5. **Cross-session facts** that tune behavior (not knowledge) go to Mem0 instead — see
   the memory-capture skill.

## Notes
- These tools can fail closed on private/internal targets or oversized responses (SSRF
  and DoS guards). If a fetch is blocked, report it plainly — do not retry against an IP.
- Prefer one well-sourced note over dumping the whole transcript verbatim into the Vault.
