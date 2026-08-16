# Installation Guide

This document guides you step by step through the installation of cogitobase on your local machine or a central VPS (Virtual Private Server).

## Prerequisites
- **[Docker](https://docs.docker.com/get-docker/) & [Docker Compose](https://docs.docker.com/compose/install/)** must be installed. The commands below use Compose V2 (`docker compose`); on an older installation that still ships V1, write `docker-compose` with a hyphen instead.
- A private GitHub repository for your Second Brain (for the automatic Git-Sync of Markdown files).
- A **Google Gemini API Key** (for the vector embedding of your knowledge, and for Mem0's implicit short-term memory).

---

## 1. Clone Repository

Clone cogitobase to your server/machine:

```bash
git clone https://github.com/mdorloechter/cogitobase.git
cd cogitobase
```

## 2. Create Git Deploy Key

So that the server can automatically push new notes to your private Second Brain repository in the background (without a password prompt), we need an SSH key. The container mounts the `./ssh/` directory read-only and expects the key at `./ssh/id_ed25519` (see `docker-compose.yml.example`), so create it there:

Create a new key on your server (without a passphrase!) and seed the known-hosts file:

```bash
mkdir -p ./ssh
ssh-keygen -t ed25519 -f ./ssh/id_ed25519 -N "" -C "mcp-bot"
ssh-keyscan github.com > ./ssh/known_hosts
```

This creates `./ssh/id_ed25519` (private), `./ssh/id_ed25519.pub` (public), and `./ssh/known_hosts`.

**GitHub Configuration:**
1. Go to your private Second Brain repository on GitHub.
2. Navigate to **Settings > Deploy keys > Add deploy key**.
3. Copy the content of the `./ssh/id_ed25519.pub` file into it.
4. **Important:** Check the box for **"Allow write access"**.

*Security note: The private key stays in `./ssh/` on your server and is mounted read-only into the container. It is git-ignored — never commit it. Seeding `known_hosts` yourself avoids blindly trusting GitHub's host key on first connect.*

## 3. Configuration (.env)

Copy the example configuration to create your real `.env` file:

```bash
cp .env.example .env
```

Open the `.env` file with a text editor (e.g. `nano .env`) and fill in the values. The variable names below are the ones the server actually reads (see `config.py` / `.env.example`):

1. **Security:**
   - `AUTH_TOKEN=` — paste the output of `openssl rand -hex 32`. The server refuses to start without it, and also rejects a token shorter than 16 characters, a non-ASCII one, or a placeholder taken from `.env.example` or these docs.
2. **Git Synchronization** *(optional — ships commented out; leave it that way to run local-only without a remote mirror):*
   - `GIT_REPO_URL=git@github.com:YourUsername/YourSecondBrainRepo.git` — uncomment and point at a repo **you** own, using an `ssh://`/`git@` URL, never an HTTPS URL with an embedded token. Pointing it at a repo you cannot clone leaves the vault without a git repo at all: the failure is logged and the server keeps serving, but nothing is committed or pushed.
   - `GIT_USER_NAME="MCP Bot"`
   - `GIT_USER_EMAIL="bot@yourdomain.com"`
   - Authentication uses the SSH deploy key mounted at `./ssh/` (see step 2). You do **not** set a `GIT_SSH_COMMAND` — the server builds it from the mounted key automatically.
   - A sync commits **everything** under `vault-data/` that `vault-data/.gitignore` does not exclude. The server seeds that file on first start with Obsidian's local state (`.obsidian/`, which can hold plugin API keys), `.trash/` and editor scratch files, then never touches it again: adapt it to your vault, and delete the `.obsidian/` line if you *want* your Obsidian setup mirrored across devices. Adding a path that is already tracked does not remove it from the remote — the server logs which files those are and leaves the `git rm --cached` to you, because doing it automatically would delete them on every other device that pulls.
3. **AI & Embeddings (Gemini):**
   - `GEMINI_API_KEY=your_google_gemini_api_key` — without it, semantic search is disabled.
   - *(Optional: to use a different model, adjust `EMBED_MODEL` and `EMBED_DIM` together — best done now, before the first start. The Qdrant collection is created with the dimension in effect at that moment and cannot be resized: changing `EMBED_DIM` later means deleting the collection `second_brain_vault` (or the whole `qdrant_storage` volume) and then running `reindex_vault`. Without that, every write fails on a vector-size mismatch.)*
4. **Mem0 (implicit memory, optional):**
   - Mem0 runs **locally** against the same Qdrant instance — there is no `MEM0_API_KEY`. See the note in step 4 about its embedding provider.

## 4. Compose File

The stack definition ships as a template, so the adjustments you make for your host (ports, memory limits) stay yours and out of git. Copy it once:

```bash
cp docker-compose.yml.example docker-compose.yml
```

`docker-compose.yml` is git-ignored; `docker-compose.yml.example` is the tracked one. After a `git pull` that changed the template, re-copy it or merge the change into your copy.

On a constrained VPS (see [ARCHITECTURE.md ADR 12](./ARCHITECTURE.md) for the 1GB-RAM use case this project targets), uncomment and size the `mem_limit`/`cpus` lines for both the `mcp-server` and `qdrant` services, so a burst of uploads or a reindex can't OOM the whole host.

## 5. Start Server

The server runs on Docker, so one command builds the image and starts the Qdrant and Python containers in the background:

```bash
docker compose up -d --build
```

Check if the server booted correctly and if there are any errors:

```bash
docker compose logs -f
```
*(With `Ctrl+C` you exit the log view again).*

If you deploy with an orchestrator other than Docker Compose (e.g. Kubernetes), set its shutdown grace period (`terminationGracePeriodSeconds`) to at least the `mcp-server` service's `stop_grace_period` — a shorter one can kill the process before a pending git sync finishes.

## 6. Connect AI Client

The server communicates using the official MCP (Model Context Protocol) over the **Streamable HTTP** transport on a single `/mcp` endpoint.

> **Which clients this endpoint serves.** cogitobase speaks the session form of the protocol, up to revision `2025-11-25`: a client opens the connection with an `initialize` handshake ([ARCHITECTURE.md ADR 15](./ARCHITECTURE.md#adr-15--protocol-era-the-initialization-handshake)). Every client below does that, and a client that also supports revision `2026-07-28` detects the era and handshakes anyway. A client that speaks *only* `2026-07-28` gets `400 Bad Request: Missing session ID` on every call — that message is the signal, and the fix is a client that supports both eras, not a setting on this server.

> The examples below use `http://localhost:8000` for a **local** connection, which the shipped compose file supports: it publishes port `8000` on `127.0.0.1` only, so the port is reachable from the host and not from the network. For access from other devices set up the [reverse proxy in §7](#7-recommended-reverse-proxy-with-nginx--https) and connect via its HTTPS URL instead.

### Example for Claude Code
Add the server to your Claude Code configuration (replace `localhost` with the IP/domain of your VPS, if it's running externally):

```bash
claude mcp add --transport http my-brain http://localhost:8000/mcp \
  --header "Authorization: Bearer YOUR_AUTH_TOKEN"
```

Note `--header` with a colon, not `-e`: `-e` sets environment variables for stdio servers, so it would register the connection without an `Authorization` header and every call would come back `401`.

### Example for Google Antigravity (agy), Cursor and OpenCode
These are separate products; the steps coincide only because each takes a remote MCP server as a URL plus headers. Enter the server in the configuration interface (or `.agents`/Settings) as an `HTTP` (Streamable HTTP) type:
- **URL:** `http://localhost:8000/mcp`
- **Headers:** `{"Authorization": "Bearer YOUR_AUTH_TOKEN"}`

They differ in how the bootstrap reaches the model — see [Auto-Loading the Context Vault](#auto-loading-the-context-vault) below for the per-client rule.

### Auto-Loading the Context Vault

cogitobase delivers your identity, AI-first rules, and skill catalog through **one tool**, `get_core_context`, which returns the full, live context in a single call. The recommended bootstrap is: **call `get_core_context` first in every new session, before acting, and again after the conversation is compacted/summarized.** A compaction is invisible to the server: the session stays open and there is no new handshake. The tool's result is also exactly the kind of content a summarization step condenses or drops, so the client has to re-pull it. Re-calling is cheap and always returns the current context. (See [ARCHITECTURE.md ADR 14](./ARCHITECTURE.md#adr-14--context-delivery-one-tool--a-startup-instructions-trigger) for the design; the `agentic_second_brain` MCP prompt returns the same context for slash-command-capable clients.)

The server also publishes a single bootstrap line — a trigger to call `get_core_context` — in its MCP `instructions` field, which spec-compliant clients auto-load on connect. It is kept to that one line on purpose: clients truncate this field to a per-server character budget, so with nothing behind the trigger there is nothing to clip. `get_core_context` then returns the full, live context (identity, rules, skill catalog, agentic workflow) in one call. Regardless, the recommended local instruction below is the reliable bootstrap path; the field is a best-effort convenience, not a guarantee. Configure each client as follows:

- **For Claude Code / Claude Desktop:** The `instructions` trigger loads on connect, but clients truncate that field — so do not rely on it alone. Add a Custom Instruction to guarantee the bootstrap and pull the *live* identity:
  > "As your first action in a new session — and again whenever the conversation is compacted/summarized — call the `get_core_context` tool of the cogitobase server and follow the returned rules."

  **For Claude Code specifically, a `SessionStart` hook is more reliable than the instruction above.** A Custom Instruction is still just text competing for priority against everything else in context, and some Claude Code configurations defer/lazily load MCP tool schemas — the tool is listed by name but not yet callable, which can cause the model to skip the call entirely instead of resolving it first. A hook is executed deterministically by the harness itself, independent of the model's instruction-following that turn. Add this to your **global** `~/.claude/settings.json` (applies to every project) or a project-local `.claude/settings.json`:

  ```json
  {
    "hooks": {
      "SessionStart": [
        {
          "matcher": "*",
          "hooks": [
            {
              "type": "command",
              "command": "bash \"$USERPROFILE/.claude/hooks/cogitobase-bootstrap.sh\""
            }
          ]
        }
      ]
    }
  }
  ```

  (On macOS/Linux, use `$HOME` instead of `$USERPROFILE`.) The hook script itself just emits `additionalContext` for the `SessionStart` event:

  ```bash
  #!/usr/bin/env bash
  cat <<'EOF'
  {
    "hookSpecificOutput": {
      "hookEventName": "SessionStart",
      "additionalContext": "MANDATORY FIRST ACTION: before responding to anything else, load and call the cogitobase MCP tool `get_core_context` (if it is only listed by name via deferred/lazy tool loading, resolve it first with ToolSearch, e.g. `select:mcp__cogitobase__get_core_context`, then call it). Apply the identity, rules, and skill catalog it returns. Do this again after any context compaction/summarization."
    }
  }
  EOF
  exit 0
  ```

  Save it as `~/.claude/hooks/cogitobase-bootstrap.sh` and make it executable (`chmod +x`). The `matcher: "*"` fires on every `SessionStart` source (`startup`, `resume`, `clear`), so the bootstrap directive is injected every time — no dependence on the model prioritizing a Custom Instruction correctly.
- **For Cursor:** Create a `.cursor/rules/cogitobase.mdc` (or `.cursorrules`) in your project:
  > "As your first action, and again after any context compaction, call the cogitobase `get_core_context` tool to load my identity and rules before executing tasks."
- **For OpenCode:** Add the same one-line instruction to your project's `.opencode/instructions.md` file.
- **For Antigravity (agy):** Antigravity ignores the MCP `instructions` field, so a local rule is the **only** bootstrap path. Add a global user rule (e.g., in `~/.gemini/GEMINI.md`):
  > "As your very first action in every new session — and again whenever the conversation was compacted or summarized and you no longer have my identity and rules clearly in view — run the cogitobase MCP tool `get_core_context` and strictly apply the returned rules. Re-calling is cheap and always current, so when in doubt re-call. Use `list_skills` for task-specific procedures."

  Because `get_core_context` returns the *complete* context in one call, this local rule stays a trivial one-liner — there is no separate `instructions.md` file to maintain.

---

## 7. (Recommended) Reverse Proxy with Nginx + HTTPS

The shipped compose file publishes port `8000` on **loopback only** (`127.0.0.1:8000:8000`), never on `0.0.0.0` — so the port is reachable from the host, and not from the network. For any deployment reachable from other devices you should put a reverse proxy in front that terminates TLS and forwards to the app. The proxy is also your first line of defence against distributed attacks (the in-app rate limiter is only a backstop — see README §5).

There are two common ways to wire Nginx in:

- **A) Nginx as a container in the same stack** (no host port for the app at all). Nginx reaches the app by its service name `cogitobase:8000` over the `internal_stack` network, so you can delete the `ports:` block from the `mcp-server` service — note that this also makes the Quickstart's `curl localhost:8000/healthz` stop working, since nothing is bound on the host any more.
- **B) Nginx on the host** (what the shipped file is set up for). The loopback mapping is already there; Nginx forwards to `http://127.0.0.1:8000`. Keep the `127.0.0.1:` prefix — without it Docker binds `0.0.0.0` and publishes the MCP endpoint to the network with the proxy bypassed.

### Nginx server block

Create `/etc/nginx/sites-available/cogitobase.conf` (host install) or mount it into your Nginx container. Replace `brain.example.com` and the certificate paths.

```nginx
# Redirect all plain HTTP to HTTPS.
server {
    listen 80;
    listen [::]:80;
    server_name brain.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name brain.example.com;

    # --- TLS (e.g. from certbot / Let's Encrypt) ---
    ssl_certificate     /etc/letsencrypt/live/brain.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/brain.example.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # The MCP Streamable HTTP endpoint.
    location /mcp {
        # A) same Docker network:   http://cogitobase:8000
        # B) Nginx on the host:      http://127.0.0.1:8000
        proxy_pass http://cogitobase:8000;

        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # Pass the client's bearer token through unchanged.
        proxy_set_header Authorization     $http_authorization;

        # Streamable HTTP keeps a long-lived Server-Sent-Events response open, so
        # response buffering MUST be off and read timeouts generous — otherwise the
        # stream is truncated or cut mid-session.
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        chunked_transfer_encoding on;
    }

    # NOTE: /healthz and /readyz are deliberately NOT proxied. The container's own
    # healthcheck probes /healthz internally over localhost:8000 (see the compose file),
    # so there is no reason to expose it to the internet.
    #
    # /metrics is likewise not exposed. It is auth-gated, but has no business being
    # reachable publicly. If you scrape it, do so from inside the Docker network or
    # restrict a dedicated location to your monitoring host:
    # location = /metrics {
    #     allow 10.0.0.0/8;
    #     deny all;
    #     proxy_pass http://cogitobase:8000/metrics;
    # }
}
```

**Enable and reload** (host install):

```bash
ln -s /etc/nginx/sites-available/cogitobase.conf /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

> **Note on git-sync health:** `/healthz`/`/readyz` cover process liveness and search readiness only — they don't reflect a paused git sync (e.g. after a rebase conflict). Monitor `mcp_git_sync_total{result="paused"}` on `/metrics`, or call the authenticated `sync_status` tool, to catch that condition (see README §7).

Your clients then connect to the **HTTPS** URL instead of the raw port, e.g.:

```bash
claude mcp add --transport http my-brain https://brain.example.com/mcp \
  --header "Authorization: Bearer YOUR_AUTH_TOKEN"
```

> **Tip:** If you enable the transport's own DNS-rebinding protection (`MCP_ALLOWED_HOSTS` in `.env`), add your public hostname there, e.g. `MCP_ALLOWED_HOSTS=brain.example.com`. Left empty, the reverse proxy remains the sole host gate — which is fine for a single-tenant setup.

---

**Done! 🎉** Your Second Brain is now online and connected.
You can now command your agent:
> *"Look in my Second Brain to see what I wrote down last time about project XYZ."* 
or
> *"Save this code snippet in my Brain."*
