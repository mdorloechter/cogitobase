# Architecture Decisions (ADRs)

This document records the **currently valid** design decisions of cogitobase and explains them. Read it before extending the server: a change that contradicts a decision here needs a new ADR, not a workaround. Implementation details and diagrams can be found in the [README](./README.md); this document focuses on the *why*.

---

## ADR 1 — Validating Server, Smart Client

**Decision:** The server does not run its own LLM and performs no autonomous routing — the *content-related* intelligence lives in the client (steered via the `agentic_second_brain` meta-prompt). However, the server is the **gatekeeper of data quality**: the non-negotiable invariants (OKF schema, required structural blocks, source/confidence markers, dedup, linking to existing notes) are **enforced server-side** (`validation.py`), not just recommended. *OKF* is the Open Knowledge Format — a Markdown file meant to read well for a human and parse reliably for a machine; ADR 5 lists what this server requires of one.

**Dividing Line:** The server checks whether something is **well-formed** (mechanically decidable), never whether it is **true/sensible** (semantically — the client's job). Examples: valid `type_meta`, correct folder for that type (a note's folder is mechanically derivable from its type, so the server derives and enforces it), present `## AI Summary` section, dates next to URLs, links to an *existing* note → Server. *What* information is noted, *which new* wikilinks make sense, truthfulness of a source → Client.

**Rationale — Future-Proofing:** As long as only a highly competent client (Claude) pulls the prompt, pure prompt-steering would suffice. But the goal is a *cross-tool* brain (Claude Code, agy, weaker models, clients that don't even retrieve the prompt). If the behavior resided only in the prompt, any such client could clutter the brain — inconsistent memories, schemaless notes. By placing the invariants in the server, the brain remains **consistent, no matter which client writes**. This trades a small amount of server logic for that guarantee (server logic costs simplicity, but validation functions are pure and synchronous, so the cost is low).

**Configurability:** The strictness is controllable via profiles (`VAULT_ENFORCEMENT=strict|balanced|lenient`) plus per-rule overrides — see ADR 6. Unambiguous rules (schema) are hard; heuristics (sources, confidence) are soft (`warn`) to avoid false-positive blocks.

---

## ADR 2 — Four Memory Pillars with Push/Pull Separation

**Decision:** Knowledge is separated into four pillars with different loading behaviors:

| Pillar | Storage | Loading |
|-------|--------|-------|
| Identity | `context-vault/` | **push** (via `get_core_context`, triggered by the startup `instructions` field — see ADR 14) |
| Skills | `skills/` | **catalog push, body pull** |
| Brain | `brain-vault/` | **pull** (semantic search) |
| Mem0 | Qdrant | **pull** (semantic search) |

**Rationale:** Identity is the *lens* through which everything else is interpreted — it must be guaranteed to be present at the start of a session (Chicken-and-egg: the AI cannot "search for you" before it knows you). Brain and Mem0, on the other hand, are reference works pulled only when needed. This separation prevents the vector DB from being cluttered with structural knowledge *and* behavioral facts: Markdown remains human- and Obsidian-readable, Mem0 holds only isolated preferences.

---

## ADR 3 — Skills: Catalog Push, Body Pull

**Decision:** Skills are a separate, git-persisted pillar. Loading is hybrid: only the **catalog** (name + `when_to_use`) is pushed into the prompt; the **body** is pulled on-demand via `get_skill`. Skills are Markdown with frontmatter (`name`, `description`, `when_to_use`, `version`); `write_skill` creates them in a versioned manner.

**Rationale:** If every skill were fully embedded in the prompt (like identity), it wouldn't scale — N skills bloat every session and dilute the context. The catalog is small and constant; the expensive body only comes when a task needs it. This keeps the server "dumb" (ADR 1), but the actual goal — a centralized, cross-tool shared procedure repository — becomes real and maintainable.

**Managed seeds:** The repo ships default-on skills (`seed-skills/`) that `seed_skills()` installs on startup. To keep them current without ever clobbering a user's edits, each installed seed is stamped with a `seed_hash` marker (the hash of the shipped content). On restart, a seed is **updated** only while it is still *pristine* (its content re-hashes to the stored marker); once the user edits it, the marker no longer matches and the file is left untouched (a newer version available is logged). A skill with **no** marker is user-authored and never touched — except a pre-managed install byte-identical to the current seed, which is adopted into management. This is the dpkg-conffile pattern: ship updates, never overwrite local changes.

**Retiring one.** `delete_skill` removes a skill; git keeps the history (ADR 4). A name the repo **still ships** is refused, because `seed_skills()` reinstalls whatever is missing on the next start — the delete would report success and then undo itself. That leaves the case the tool exists for: seeding iterates the *shipped* seeds only, so a seed dropped from the repo (a rename, say) stays behind as a managed file the catalog still advertises, and it is now removable through the same channel that installed it. A vanished seed is deliberately not deleted automatically — it is indistinguishable from a skill the user wrote, and the catalog is the instruction channel, so guessing wrong costs a procedure.

---

## ADR 4 — Git as Single Source of Truth

**Decision:** The Markdown files are the source of truth; Qdrant is just a rebuildable index. Every write operation triggers a Git synchronization.

**Rationale:** A vector DB or a server can fail or be deleted and be rebuilt from the `.md` files; the reverse does not hold. Git provides a diff-able, human-readable backup with per-change history, and Obsidian syncs against the same working tree with no export step.

**Implementation:** Write tools do not block on the push, but place a job in an `asyncio.Queue`; **one** worker serializes the Git operations (no race between parallel write accesses and the cron). On a rebase conflict, local work is parked on a `conflict-*` branch and the sync is **cleanly aborted** — nothing is discarded, and the diverged branch is not pushed (no endlessly failing sync).

**Editing a file the server does not own.** The vault is a working tree Obsidian writes to as well, so a note reaching a write tool is not necessarily one the server produced. An edit therefore touches only what it means to change and carries the rest over as **text**: `append_to_note` splits the file at its frontmatter block, sets `updated`/`type` on those lines in place, and leaves every other byte — the body, the block's key order, each value exactly as typed — where it was. Parsing the file and re-serialising it would rewrite metadata nobody asked it to: YAML reads `10:30` as 630, `0123` as 83, `1.10` as 1.1, `no` as false, and reorders the block on every append. It also means a leading `---` only counts as frontmatter when it parses to a non-empty mapping, because a horizontal rule, a block emptied out in Obsidian's properties panel, a comment-only block and a YAML list are indistinguishable to a parser — all of them stay body. A block that *is* meant as frontmatter but is not valid YAML gets the note refused by name instead: it cannot be edited without either duplicating or dropping it.

**Where an append lands.** `append_to_note` takes an optional `section` and files the text at the end of *that* section — from its heading to the next heading of the same or a higher level, ignoring anything inside a fenced code block, since a snippet's `# install` is a comment. Appending to the file end instead files it under whichever section happens to be last, which stops being the intended one the moment the author adds one below; `capture_session_retro` names `## Retros` for exactly that reason, because the note's own summary claims its entries live there. A `section` the note does not hold is created at the end and reported, rather than refused — the caller is a capture tool at the end of a session, and losing the text costs more than a heading the author has since renamed. Existing prose is never rearranged: a note whose entries already sit elsewhere keeps them there, because moving an author's text is not an edit the server was asked to make. Text is separated by exactly one blank line, without which Markdown reads a following `## Heading` as part of the preceding paragraph.

**Index convergence.** "Rebuildable" is the invariant that makes the index safe to be wrong: every divergence is repairable from the files, and `reindex_vault` is that repair (serialised — one rebuild at a time, since a second concurrent run only re-embeds what the first already wrote). Within that, the write path still converges on its own, because a stale index is not merely incomplete but actively *misleading* — a point carries the chunk's plaintext, so an orphan keeps serving prose the note no longer holds:
- A point id is derived from `(vault-relative path, chunk_idx)`, so re-indexing **replaces** a note's chunks in place; a note that shrinks additionally has its surplus tail deleted, after the upsert (never before, so the note is not momentarily unfindable).
- `rename_note` renames the file first, then moves the index. A failed rename therefore leaves a note that is still fully searchable; deindexing up front would strip a note that is still on disk of every vector.
- Indexing re-checks containment immediately before the upsert, not just when the file was picked up: a rebuild spends minutes in embedding latency, and the file may have been deleted or replaced by a symlink meanwhile.
- One chunk, one vector: `index_markdown_file` refuses to index at all when the embedder returns a different count, because `zip` would otherwise pair the leading chunks and leave the note's tail unfindable while the write reports success. A partial index is worse than none — search answers, and the answer looks complete.

**The embedder's batch shape is checked against the real SDK, not a stub** (the second place a test refuses a mock, alongside ADR 6's mem0 keyword guard). `google-genai` groups *consecutive* plain items in `contents` into a single `Content`, so a flat list of N chunk strings is one document of N parts and comes back as one vector — a malformed request that presents as a broken response. The adapter therefore wraps each item in its own list, which is the shape the SDK reads as a document boundary. A stub cannot decide this: it returns one vector per item because that is what the caller means, so it agrees with the caller whichever shape is sent, and every note long enough to chunk would silently fail to index while short ones kept working. `test_a_batch_of_chunks_is_one_document_each` runs the adapter's shape through the SDK's own normaliser and counts the documents that come out. The startup probe embeds **two** items for the same reason — one cannot show batching, and most notes fit in a single chunk.

**What a search hands back.** A vector search has no notion of "no match": Qdrant fills the requested `limit` with the nearest neighbours it has, however far away they are, so an unanswerable query still returns files. `search_vault` therefore prints each hit's cosine score alongside it, and the client — which is obliged not to claim absence without an exhaustive search (ADR 5) — reads whether it found an answer or the index's best padding. It is not filtered by a threshold: dropping a weak hit would put that judgement in the server and hide the very signal, the same reason a Mem0 near-duplicate is reported rather than suppressed (ADR 6). `limit` is the caller's, bounded at 20 and clamped in code rather than trusted from the schema, because a JSON Schema bound is advertised to the client and not enforced by the transport.

Where convergence *cannot* be automatic, it is refused rather than half-done: an `EMBED_DIM` change makes every existing vector the wrong size, and Qdrant collections cannot be resized — so the collection must be dropped before a rebuild, which is an operator action (documented in `INSTALL.md`), not something a tool does to a live index.

---

## ADR 5 — AI-First Vault Rules (Server-Side Enforced)

**Decision:** Notes are primarily written for "Future Claude". Mechanically testable rules are enforced server-side in `validation.py` (not just recommended via prompt):

| Rule | Enforcement |
|-------|--------------|
| OKF frontmatter complete (date/type/tags/ai-first) | **hard** — Server owns the schema, client cannot forge it |
| An H1 title is present | **hard** — the server prepends one when the body brings none; a body that brings its own keeps it, so several H1s are not rejected |
| `type_meta` ∈ predefined types | **hard** |
| Folder DERIVED from `type_meta` (`type` == folder), `vault` must match the type | **hard** — Server files the note; client passes a bare filename, folder prefixes ignored |
| A name that matches several notes is not resolved by the server | **hard** — `read_note`/`append_to_note`/`rename_note`/`delete_note` return the candidates as vault-qualified paths; the client resends one |
| `## AI Summary` preamble present | hard (`strict`) / soft (`balanced`) |
| Source URLs with date/recency marker | soft (`warn`) — heuristic |
| Confidence marker for speculation/sources | soft (`warn`) — heuristic |
| Linking to **existing** notes (title matching) | `auto` (insert) / `warn` (suggest) — never blocking; a title held by several notes is named, never linked |
| Writing leaves a name shared by several notes | soft (`notice`) — `write_note`/`rename_note` name the others and their types |
| No claim of absence without exhaustive search | prompt only (not mechanically testable) |

**Rationale:** Makes the Vault a reliable context store rather than a loose note collection — regardless of which client writes (ADR 1). Heuristic rules remain soft so that false positives never block a legitimate write. Every hard rejection returns actionable correction instructions so the client doesn't repeat the same invalid input.

**Naming one note.** Because the folder carries the type, the same basename under two folders is two notes of two types — a legitimate state, not a collision. `write_note` always knows which is meant (`type_meta` is required); the tools that address an *existing* note get only a name. Where that name fits several notes, the server refuses to guess and hands back the candidates as vault-qualified paths (`brain-vault/projects/roadmap.md`), in which the folder segment **is** the type. Every tool that *emits* a note name uses that one form — `search_vault` hits, `list_notes` entries, the dedup notice — so anything the client sees can be sent straight back. Media files address no note and keep their basename. The cost of the state is announced where it is incurred (a notice on the write that creates it) and never silently papered over: a `[[title]]` matching several notes resolves nowhere in particular, so it is reported instead of inserted. Throughout, the server supplies facts and the client decides (ADR 1, "Smart Client").

---

## ADR 6 — Mem0 & Vault: Soft/Hard Enforcement by the Same Pattern

**Decision:** Memory and Vault write accesses follow the same enforcement pattern — **hard** where structure is unambiguous; **soft** where data loss threatens:
- **Mem0 hard:** `add_memory` rejects facts without a behavioural category prefix (`[Preference]`, `[Constraint]`, `[Explicit]`, `[Inferred]`) or exceeding the length limit. Content facts (tech/project/person) belong in the Vault, not Mem0.
- **Mem0 hard (exact repeat):** the same search that finds similar memories also refuses a fact the store already holds word for word, naming the existing ID. The write stores the text verbatim (`infer=False`), which is what keeps the enforced prefix in the store — Mem0's extraction path would rewrite the text and drop it — but that path is also where Mem0's own hash deduplication lives, so this check replaces it. It stays non-fatal: a search that is down lets the write through, because a lost check costs a duplicate the caller can delete while a blocked write costs the fact.
- **Mem0 soft:** before the add, similar memories are searched and their IDs returned ("consider update_memory").
- **Vault soft (Dedup):** before `write_note`, the server looks for the most similar existing note; from `VAULT_DEDUP_SOFT` it reports it ("consider append_to_note"), from `VAULT_DEDUP_HARD` (opt-in) it rejects as a near-duplicate. The candidate is named — and the note being written excluded — by vault-relative path, the index's identity key: the reported duplicate is therefore directly addressable (the hint is actionable), and a *different* note of the same basename is compared rather than skipped.
- **Vault overwrite:** `write_note` never overwrites silently — an existing note requires `overwrite=true`.

**Rationale:** Hard blocking of "duplicates" would prevent legitimate refinements (data loss). Soft enforcement gives the client the facts (similar IDs) and leaves the decision to them — conforming to "Smart Client" (ADR 1). This prevents contradictory states ("likes Python" / "hates Python") that poison future sessions.

**One test on this boundary uses the real library, not a mock.** Every `mem0.Memory` method ends in `**kwargs`, so a keyword the library does not know is dropped rather than raised: the call returns normally, the store applies its own default, and the argument this server thought it was passing decided nothing. A mock cannot detect that, because a mock is written from the call site — its signature mirrors what we send, so it agrees with us by construction and a wrong name stays invisible for as long as the suite is green. `test_every_mem0_keyword_is_one_mem0_reads` therefore imports `mem0.memory.main.Memory`, reads the call shapes out of `memory.py` with `ast` rather than listing them (a list would be a second claim about the same calls, free to drift from them for the same reason a mock is), and `bind()`s each one against the real signature; anything landing in `**kwargs` fails. The mocks elsewhere stay — what they test is this server's enforcement, which is ours — but their signatures are written as narrow as the library's, so a call a fake accepts is one the real client accepts too.

---

## ADR 7 — Augmentation Tools: Read-Only + SSRF-Safe Fetch

**Decision:** Network-capable tools (`search_web`, `ingest_media`, `analyze_github_repo`) are allowed, but strictly **read-only** — they write nothing autonomously into Vault/Mem0; the AI decides afterwards via `write_note`. Every HTTP(S) fetch goes through `security.safe_fetch`, without exception.

**Rationale — Server Exposure:** Unlike the source skill (client-side), this server is exposed, making the fetches an SSRF vector. `safe_fetch` validates every URL (`validate_external_url`: only http/https, DNS resolution, blocking private/loopback/link-local/metadata IPs, optional allowlist), **does not follow redirects automatically** but revalidates every hop and connects to the exact validated IP (IP-pinning, closing the redirect and DNS-rebinding bypasses) and enforces a byte limit. Remaining read-only also preserves the "Dumb Server" principle (no autonomous routing, only data gathering).

**`analyze_github_repo` reads, it does not clone:** the structure comes from GitHub's tree API (`api.github.com/repos/{owner}/{repo}/git/trees/HEAD?recursive=1`), the README from `raw.githubusercontent.com`. Two `safe_fetch` calls, so IP-pinning and the allowlist apply as everywhere else, nothing is executed and nothing is written to disk. This is why the tool is limited to `github.com`: it reads one forge's API, and a URL it cannot serve is refused by name rather than attempted. An `AUGMENT_ALLOWED_DOMAINS` deployment must allow `api.github.com` and `raw.githubusercontent.com`, not `github.com`.

**Output discipline (all three tools):** these tools feed a context window, so their output is bounded — and a cut names its full size rather than passing for the whole: `Repository Structure (300 of 4213 files)`, `Article Content (first 50000 of 214883 chars)`. GitHub's own `truncated` flag for very large trees is passed on too. A result presented as complete when it is not invites the caller to reason about a repo or an article it never saw — the same anti-fabrication concern the vault enforces for notes. The bound is in the unit that matters: `MAX_INGEST_CHARS` counts characters of text, while `MAX_FETCH_BYTES` bounds the download that produced it.

---

## ADR 8 — Single-Tenant by Design

**Decision:** The server is deliberately designed for **one** user (`MEM0_USER_ID`, default `admin`). No multi-tenancy, no user management.

**Rationale:** It is a *personal* server. Multi-tenancy would be over-engineering and massively complicate auth/isolation.

---

## ADR 9 — Modular Architecture with Tool Registry

**Decision:** Instead of a `server.py` monolith with a huge `call_tool` `if/elif` chain, logic is split into focused modules (`config`, `registry`, `security`, `clients`, `git_sync`, `vault`, `memory`, `augment`, `skills`, `server`). Tools register via `@register(name, description, schema)` and are dispatched centrally. Modules read `config.X` at runtime.

**Rationale:** A "brain" that is supposed to grow with skills and tools must not collapse into a single file. Adding a tool is now a local change (a decorator). Runtime reading of `config` plus the clients degrading to `None` (`clients.py`) make the modules individually importable and testable — even without the heavy ML stack.

**A refusal's text is part of the contract.** The caller is a model, and the reply is all it gets: there is no status code it can branch on and no second channel to consult, so whatever the text omits is simply unavailable to it. Three things therefore belong in every refusal — what was refused, why, and what to do next. A miss echoes the name it was given (a caller working from memory cannot otherwise tell a typo from something never written) and names a tool that produces a real one. A limit refusal names the actual value and the ceiling, plus the env knob that moves it, because "too large" alone does not distinguish "shrink what you sent" from "ask the operator for more". An unavailable dependency leads with the action it blocked, not the product's name — the caller never chose the store, and "Mem0 offline." does not say whether the write landed. What a refusal must NOT be is a status word in prose: the `outcome` label carries the classification, so a `Rejected: ` prefix only spends the caller's attention on something the metric already knows.

**A schema is documentation, not just validation.** The same reasoning runs one step earlier: before its first call the caller has only the tool list, so a parameter's name and type are the whole of what it knows to send. Types constrain shape and say nothing about content, which is where this server puts most of its rules — `content` must open with an `## AI Summary`, a `fact` needs a behavioural prefix, a `project` selects which note an entry is filed in. Left undescribed, each is a guess resolved by a refusal, which is a round trip spent learning something the listing could have said once. Every *required* parameter therefore carries a description, with no exemption for the ones that look obvious: the exemption list is the failure mode, since a parameter joins it by being overlooked rather than by being self-evident, and a test over the registry admits none. Optional parameters are left to judgement — omitting one is always valid, so the caller is never forced to guess. A description's job includes the consequence a name hides: that a delete also drops the note from the search index and commits to the shared mirror, or that a rename leaves incoming `[[wikilinks]]` dangling.

**Every tool is introduced somewhere.** Tool descriptions are a reference the model consults once it has a tool in mind; they cannot prompt it to consider one. That job belongs to the two push channels — the meta prompt and the skill catalog (ADR 14) — plus the seed skills a matching task pulls. A tool named in none of them is reachable only by reading the raw listing, which is what the push channels exist to spare the model, so a test requires every registered tool to appear in a seed skill or the meta prompt. It is deliberately a mention test and not a quality one: whether the mention is *good* is a review question, but whether it exists is mechanical, and absence is the failure that recurs — a new tool arrives with a description and no reason for anyone to call it.

**Who classifies an outcome.** The dispatcher can only see whether a handler returned or raised, and in this server a refusal is a normal return: MCP has no error channel a model reads well, so every "no" comes back as text the model can act on. The handler therefore labels its own reply — `text(message, OUTCOME_REJECTED)` or `OUTCOME_UNAVAILABLE`, carried to the dispatcher on a `ToolResult` (a `list` subclass, so the MCP layer and every handler signature stay untouched) — and only `ok`/`error` remain the dispatcher's. Central dispatch classifying it instead would mean reading the reply's prose, which makes a wording nobody treats as a contract into the definition of a metric: rewording a message moves it between outcomes, and any refusal the dispatcher's list does not anticipate is counted as a success. The two labels are deliberately about *who can act*, not about severity: `rejected` is the caller's to fix, `unavailable` is the operator's, and separating them is what lets a dead Qdrant be distinguished from a busy day of malformed requests. A completeness test walks the AST of the tool modules and fails on a `text()` call that carries neither a label nor a place on an explicit list of genuine successes, so the classification stays exhaustive as tools are added.

---

## ADR 10 — Minimally Invasive Integrations (KISS)

**Decision:** For third-party systems, the simplest, least error-prone path is chosen: prefer stateless, read-only fetches over stateful OAuth flows, and let the AI decide what to persist afterwards. The augmentation tools (`search_web`, `ingest_media`, `analyze_github_repo`) follow this — each pulls data through the SSRF-safe `safe_fetch` and writes nothing autonomously.

**Rationale:** Fulfills the business purpose (the AI can gather external context on demand) while drastically reducing technical complexity, credential handling, and maintenance burden. An integration that would require persistent tokens or a callback flow must clear a much higher bar to be added.

---

## ADR 11 — Ingress Limits: Process-Local, Not Distributed

**Decision:** The server enforces a request body limit (`413`) and a per-client rate limit (sliding window, `429` + `Retry-After`) at the MCP ingress (after auth) via a `ProtectionMiddleware`. The limiter (`ratelimit.py`) is **in-memory and process-local**, dependency-free, with an injectable time source.

**Rationale:** It is a DoS *backstop*, not a distributed protection. Fitting the single-instance/single-tenant deployment (ADR 8), there is no need for Redis or similar — that would be over-engineering (ADR 10). Distributed attacks are repelled by the upstream reverse proxy; the middleware protects against what happens *behind* the auth: a runaway client, a leaked token, a loop. The key is the **hashed** token (never logged raw) — and it can only be, because the 401 is returned before the limiter is consulted, so an unauthenticated caller never reaches it. Keying such a caller would mean keying by peer IP, which behind a reverse proxy is the proxy itself: one shared bucket in which the first caller locks out the rest. Unauthenticated request rates therefore belong to the proxy, which knows the real client address. Idle keys are periodically pruned so memory doesn't leak. All thresholds can be disabled/adjusted via ENV.

---

## ADR 12 — Gemini API and Native Multimodality

**Decision:** Serverless embedding via the Google GenAI API (`gemini-embedding-2`), enabling **native multimodality**:
- Images linked in Markdown (`![alt](../media/image.jpg)`) are automatically loaded from disk during indexing and sent to Gemini along with the text.
- Uploaded PDFs are natively vectorized as a whole.
- Vectors are made searchable along with metadata in the `media` vault in Qdrant.

**Rationale:** The memory consumption on small VPS instances (e.g. with only 1GB RAM) blocked operations. Offloading to the cloud massively relieves the server. At the same time, the Gemini model is multimodal-capable, so the "Second Brain" can conceptually understand and retrieve images and PDFs without complex OCR workarounds.

---

## ADR 13 — Transport: Streamable HTTP (not SSE)

**Decision:** The server speaks MCP over the **Streamable HTTP** transport on a single `/mcp` route (POST for JSON-RPC, GET for the response stream, DELETE to end a session), via `StreamableHTTPSessionManager`. The session manager is (re)created per serving window inside the Starlette lifespan, because its `run()` may be entered only once per instance. DNS-rebinding protection (Host/Origin allowlist) is available in the transport but **off by default** — the reverse proxy is the first line of defence (ADR 8 / ADR 11).

**Rationale:** Streamable HTTP replaces the split HTTP+SSE transport of protocol revision `2024-11-05` and is the transport every MCP revision from `2025-03-26` onward defines. The single-endpoint model is simpler to place behind a reverse proxy, supports stateless operation and configurable idle timeouts, and is what the clients cogitobase targets negotiate. The `ProtectionMiddleware` (auth, body cap, rate limit) sits in front of `/mcp`; the streaming body-cap check (no `Content-Length`) does not interfere with the SSE response stream. Which *shape* of Streamable HTTP the server speaks — sessions and a handshake, or per-request metadata — is ADR 15.

---

## ADR 14 — Context Delivery: One Tool + a Startup `instructions` Trigger

**Decision:** The "push" pillars (Identity + skill catalog + AI-first rules) are delivered by exactly **one content source** (`skills.build_prompt` and its parts) over **two thin delivery paths**, neither of which duplicates content in source:

1. **`get_core_context` tool** — returns the *full, live* context (meta-rules + fresh identity + fresh catalog) in a single call. This is the authoritative path and is always up to date (it re-reads the vault on every call).
2. **MCP `instructions` field** — set once per serving window in the lifespan (after `seed_skills`) to the bootstrap trigger alone: a short paragraph telling the client to call `get_core_context` first. It carries **no** rules, catalog or identity, because clients truncate this field to a per-server budget — with nothing behind the trigger, there is nothing that can be clipped, and nothing live-editable is frozen until a restart.

Identity is delivered as one tool call rather than as an MCP resource requiring a multi-step `list_resources → read_resource` iteration; skill **bodies** stay pull-only via `get_skill` (exposing them as resources would let a resource-iterating client bypass the "catalog push, body pull" rule of ADR 3). The `agentic_second_brain` MCP prompt is kept for slash-command-capable clients and is fed from the same `build_prompt`.

**Rationale — the bootstrap problem:** MCP clients build their system prompt locally. Empirically, no major client auto-loads MCP *prompts*, and *resources* are only fetched when a task drives the client to enumerate them — so neither reliably delivers "push" content. The only channel a spec-compliant client (e.g. Claude Code) auto-loads on connect is the `instructions` field. We therefore put the trigger there so those clients are never blind, and deliver everything else — rules, catalog, live identity — through the tool, so a vault edit is never stale and a truncated `instructions` field costs nothing. Clients that ignore `instructions` (e.g. Antigravity) need a **local one-line rule** that triggers `get_core_context`; because the tool is self-sufficient (returns the *full* context, not just the fresh identity), that local rule stays a trivial trigger rather than a second copy of the content. The unavoidable residue — a client that both ignores `instructions` and whose local rule fails to fire — is at least made observable: `get_core_context` is counted in `mcp_tool_calls_total`, so a missing bootstrap shows up in `/metrics` instead of failing silently.

The `instructions` field survives the era split of ADR 15: `DiscoverResult` carries it too, so the trigger reaches a modern client through `server/discover` rather than through the handshake.

---

## ADR 15 — Protocol Era: the Initialization Handshake

**Decision:** cogitobase serves the **initialization era** of MCP — protocol revisions up to and including `2025-11-25`, in which a client opens a session with an `initialize` handshake and the transport tracks it under an `Mcp-Session-Id`. The mechanism is the exact pin `mcp==1.28.1` in `requirements.txt`: the 1.x SDK line implements that era and nothing else. Serving the `2026-07-28` era is a separate decision, not a configuration change, and it is not made here.

**What the eras differ in.** Revision `2026-07-28` removes the session from the protocol: no `initialize`, no `Mcp-Session-Id`, no GET stream, no `Last-Event-ID` resumability, and no server-initiated JSON-RPC requests. A request carries its own protocol version, client identity and capabilities in `_meta.io.modelcontextprotocol/*`, mirrored into the `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` headers; server identity and `instructions` move to `server/discover`; long-lived notifications move to one `subscriptions/listen` response stream.

**Rationale — why the pin is the whole decision.** The spec defines the client side of the split so that a **dual-era client** finds its way: it tries a modern request, inspects a `400`, and falls back to `initialize` when the body is not a recognized modern error. cogitobase answers such a probe with `-32600 Bad Request: Missing session ID`, which is not a modern error, so the fallback fires and the client works. A **modern-only** client does not connect at all. The exposure is therefore bounded by client adoption, and it is observable from outside: a `400` on `/mcp` from a client that never handshakes is what it looks like.

Crossing to the modern era means replacing the SDK, not flipping a switch. SDK v2 serves both eras from one app with nothing to configure, but its API is a hard fork: handlers move from decorators to constructor `on_*` parameters, result types are no longer auto-wrapped, tool exceptions become JSON-RPC errors instead of error results, and every field is renamed to snake_case. `pip install mcp` resolves to 2.x, so the exact pin — not a range — is what keeps a rebuild reproducible.

One thing the era split does **not** cost us: cogitobase initiates nothing back to the client. It uses no sampling, no elicitation, no roots and no resource subscriptions, which is the surface `2026-07-28` reshapes most and the reason most servers cannot go stateless. That also makes `MCP_STATELESS` (ADR 13) a free setting rather than a trade-off.
