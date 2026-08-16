# Security Policy

cogitobase is a self-hosted MCP server that is deliberately exposed to the network, so security reports are taken seriously.

## Reporting a Vulnerability

**Please do not open a public issue for security vulnerabilities.**

Instead, report privately via GitHub's **[Security Advisories](../../security/advisories/new)** ("Report a vulnerability"), or e-mail the maintainer listed in the repository profile.

Please include:
- A description of the vulnerability and its impact.
- Steps to reproduce (a proof-of-concept if possible).
- The affected version / commit.

You can expect an acknowledgement within a few days. Once a fix is available, we will coordinate a disclosure timeline with you and credit you (unless you prefer to stay anonymous).

## Scope & Threat Model

cogitobase is **single-tenant** and expects to run **behind a reverse proxy that terminates TLS** (see `INSTALL.md` §6). The relevant trust boundaries:

- **Ingress:** bearer-token auth (fail-closed), request body cap, and a per-client rate limit sit in front of the `/mcp` and `/metrics` endpoints. The body cap (`MAX_REQUEST_BODY_BYTES`, 4 MB) is what actually bounds an upload: `upload_media`'s own limit (`MAX_MEDIA_BYTES`, 10 MB decoded) is reached only once the cap is raised or switched off, since base64 inflates a payload by 4/3. The health endpoints are deliberately outside that (see the limitations below). The reverse proxy is the first line of defence against distributed attacks; the in-app limiter is a backstop.
- **Outbound fetches** (`search_web`, `ingest_media`, `analyze_github_repo`) pass through an SSRF filter (`security.py`): http/https only, DNS resolution with private/loopback/link-local/metadata IP blocking, and — wherever the fetch itself goes through `safe_fetch` — IP-pinning against DNS-rebinding plus no automatic redirect following. The domain allowlist (`AUGMENT_ALLOWED_DOMAINS`) binds every one of them. `analyze_github_repo` reads GitHub's tree API and `raw.githubusercontent.com` through `safe_fetch`, so an allowlist deployment must name **those two hosts**, not `github.com`. Note that `search_web` takes a *query*, not a URL: the server sends it to DuckDuckGo and then fetches the URLs that come back (`max_results`, default 3, capped at 10), so the filter — not the caller — is what decides where those go (`PRIVACY.md` lists it as its own egress path). One consequence is worth stating plainly: **setting `AUGMENT_ALLOWED_DOMAINS` effectively disables `search_web`'s extracts**, because a search returns arbitrary domains and nearly all of them will be refused. The query still leaves the host — it goes to DuckDuckGo, which is not a `safe_fetch` call and so not subject to the allowlist. An allowlist deployment should treat `search_web` as a URL discovery tool and use `ingest_media` on the allowed domains.
- **File access** is confined to the vault via `validate_safe_path` (`resolve()` + `is_relative_to`). Lookups that reach a file by glob rather than by an explicit path go through `is_contained_file`, which rejects symlinks and re-checks the resolved path — `is_file()` alone follows a link out of the vault.
- **Symlinks in the vault** are a distinct entry point, because the vault is a git working tree (ADR 4): a `120000` blob committed to the mirror repo materialises as a link on the next clone or pull, so planting one needs commit access to the mirror (or a second Obsidian client), not shell access to the host. The vault repo is therefore configured with `core.symlinks=false` — git writes a plain file holding the target path instead of a usable link — and the readers guard themselves independently.
- **Secrets** (`AUTH_TOKEN`, `GEMINI_API_KEY`, the SSH deploy key) are injected at runtime and must never be committed. See `.gitignore` / `.dockerignore`. `AUTH_TOKEN` is validated at import: the server refuses to start on an empty, too-short (< 16 chars), non-ASCII, or placeholder value, so an unedited `.env.example` cannot boot a network-facing server behind a token that is public in every clone. `GIT_REPO_URL` is validated too: a URL carrying a credential is refused, because git echoes the remote URL in its error output and the secret would reach the logs in plaintext.

### Known accepted limitations
- `ingest_media`'s YouTube branch validates the URL (scheme, allowlist, private-IP block) but hands the fetch to `youtube-transcript-api`, which resolves the host itself — so IP-pinning and redirect re-validation do not cover it, leaving a narrow TOCTOU window. It is the only fetch in the server that is not IP-pinned; acceptable for the single-tenant/trusted-operator model. Front a public multi-tenant deployment with an egress allowlist.
- `/healthz` and `/readyz` sit outside the authenticated prefixes on purpose, so an orchestrator can probe them without holding the bearer token — which also puts them outside the body cap and the rate limit. `/readyz` is the one with a cost: each call pings Qdrant in the shared thread pool, so a flood of requests competes with indexing work. Neither leaks more than liveness (the body is `ready` / `not ready`, never which dependency is down). Rate-limit both at the reverse proxy; the shipped compose publishes port 8000 on loopback only, so nothing reaches them without passing it first.
- Content is sent to the Google Gemini API for embedding and, for Mem0, processed locally against Gemini. See `PRIVACY.md` for the full data-flow.

## Supported Versions

This project is pre-1.0; security fixes are applied to the latest `main`. Pin a commit for reproducible deployments.

## Dependencies

`requirements.txt` pins an exact version for every **direct** dependency. Their transitive dependencies are not pinned — the file lists around a dozen packages, while a resolved install brings in well over a hundred — so two builds from the same commit can differ below that first level. The pins make the direct set reproducible and deliberate to change; they are not a lockfile.

CI runs `pip-audit -r requirements.txt` as a separate job on every push and pull request and **fails the build** on a known advisory, so a direct pin cannot silently go stale. That scope follows from the above: auditing the requirements file resolves and checks the transitive tree as it stands at that moment, so a finding in a package the file does not name is reported but cannot be fixed by editing it. The pin to bump is the direct dependency that pulls it in. Clear a finding by bumping that pin, or, if the vulnerable code path is unreachable here, with an `--ignore-vuln <id>` that carries a written rationale.
