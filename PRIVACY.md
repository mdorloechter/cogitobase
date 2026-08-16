# Privacy & Data Flow

cogitobase stores a highly personal "Second Brain". This document enumerates **every place your data lives or travels**, so you can make an informed decision before pointing it at sensitive content.

## Where your data lives

| Store | Location | Contents |
|---|---|---|
| Markdown vault | `vault-data/` on your server (source of truth) | Full note text, identity/context files, skills |
| Git remote | Your **private** GitHub repo (if `GIT_REPO_URL` is set) | A mirror of the vault, including full history — everything under `vault-data/` except what `vault-data/.gitignore` excludes |
| Qdrant | `qdrant_data` volume on your server | Embedding vectors **and a plaintext copy of each chunk's text** in the payload |
| Media | `vault-data/media/` | Uploaded images / PDFs |

Nothing above leaves your infrastructure except the Git remote (which you control and which should be private).

## What leaves your server (third-party egress)

| Destination | What is sent | When |
|---|---|---|
| **Google Gemini API** | The text of every note/chunk (and image/PDF bytes) to be embedded, plus every search **query** | On write/index and on `search_vault` |
| **Google Gemini API** (via Mem0) | Atomic facts you store with `add_memory`, for embedding. The text is stored verbatim, so it is not also sent to an LLM to be rewritten | On `add_memory` / memory operations |
| **DuckDuckGo** | The `search_web` **query** — the search terms themselves, not just a URL | On `search_web` |
| **The open web** | The URLs you pass to `ingest_media`, **plus the result URLs DuckDuckGo returns for a `search_web` query** (`max_results`, default 3, at most 10), which the server then fetches and extracts on its own | On demand, SSRF-filtered (see SECURITY.md for one narrow limitation) |
| **GitHub** (`api.github.com`, `raw.githubusercontent.com`) | The owner and repo name you pass to `analyze_github_repo`. Unauthenticated, so no token of yours is sent — but the request carries your server's IP and is subject to GitHub's 60-requests/hour limit per IP | On `analyze_github_repo` |

> **Important:** cogitobase does **not** use OpenAI. Mem0's LLM and embedder are pinned to Gemini (`clients.py`); if no `GEMINI_API_KEY` is set, Mem0 is disabled rather than silently falling back to a third party.

### Bundled telemetry (disabled)
The `mem0ai` dependency ships opt-out PostHog telemetry that would fire on every server start and send a stable install UUID plus host/OS/CPU and collection metadata (no note text) to `us.i.posthog.com`. cogitobase sets `MEM0_TELEMETRY=False` before importing Mem0, so that request never happens. Setting `MEM0_TELEMETRY=True` in your environment opts back in and adds `us.i.posthog.com` as a further egress destination to the table above.

### Gemini data-use tiers
Data sent to the Gemini API is subject to Google's terms. The **free tier may use your content to improve products**; **paid / Vertex AI tiers do not**. For a sensitive Second Brain, use a paid API key or Vertex AI. This is a deployment choice you must make — the software cannot enforce it.

## Right to erasure — limitations

Deletion is **not** a full purge; know the caveats:

- `delete_note` removes the file, its Qdrant vectors, and commits the deletion — **but the content remains in the Git history** on the remote. To truly purge, run `git filter-repo` (or equivalent) on the remote and force-push.
- `delete_memory` removes the memory from active use, but Mem0 may retain an entry in its local change-history store not reachable via any tool.
- Qdrant stores a plaintext copy of chunk text; `delete_note` removes that note's points, but back up/snapshot copies (if you make them) are your responsibility.
- Removing a passage from a note (via `write_note … overwrite=true`) reindexes it and drops the chunks the shorter note no longer fills, so the removed prose leaves the index too. This requires the embedder and Qdrant to be reachable: if the reindex fails, the old chunks stay searchable until the next successful write or `reindex_vault`.

## Logging

Structured logs (`observability.py`) are written to **stderr** and never contain note bodies, tokens, or memory content. Note **filenames** (which are user-chosen titles) and tool names do appear in logs for correlation. Client identities are logged as a **hashed** token, never the raw bearer token. Set your own log retention policy accordingly.

The log line for a failed git operation carries git's own error output, which includes the remote URL, the repo name, and the deploy-key path. There is no redaction filter on that text; instead the server refuses to start on a `GIT_REPO_URL` that embeds a credential, so no secret is present to be printed. Keep credentials out of the URL (use the SSH deploy key) and this holds.

## Recommendations

1. Keep the Git remote **private**.
2. Use a **paid Gemini / Vertex** key for sensitive data.
3. Keep Qdrant bound to the internal Docker network (default) and consider enabling its API-key auth.
4. Treat `vault-data/` and any backups as containing plaintext personal data — encrypt at rest if your host isn't trusted.
