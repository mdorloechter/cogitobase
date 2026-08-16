"""Central configuration, constants and logging for the cogitobase MCP server.

Everything that other modules need to read from the environment lives here.
Modules import this module (``import config``) and reference attributes at
call-time (``config.BRAIN_DIR``) so that tests can monkeypatch a single source
of truth.
"""
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()

# --- LOGGING (stderr, never stdout — stdout stays reserved for protocol) ---
# Structured JSON logging + request correlation lives in observability.py.
# LOG_FORMAT=json|text, LOG_LEVEL=INFO control the output.
import observability  # noqa: E402

log = observability.configure_logging()

# --- VERSION ---
# The one place the version is written. server.py hands it to the MCP handshake, so a
# client can tell which build it is talking to; left unset, the SDK reports its OWN
# version there instead and every deployment looks identical.
__version__ = "1.0.0"

# --- CONFIG ---
AUTH_TOKEN = os.environ.get("AUTH_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
QDRANT_HOST = os.environ.get("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
GIT_REPO_URL = os.environ.get("GIT_REPO_URL")
GIT_USER_NAME = os.environ.get("GIT_USER_NAME", "MCP Server")
GIT_USER_EMAIL = os.environ.get("GIT_USER_EMAIL", "mcp@local")
MEM0_USER_ID = os.environ.get("MEM0_USER_ID", "admin")  # single-tenant by design

# Mem0 enforcement parameters
MEM0_MAX_FACT_CHARS = int(os.environ.get("MEM0_MAX_FACT_CHARS", "280"))
# Mem0 holds BEHAVIOUR (dispositions that tune future sessions), never CONTENT
# (facts about the world / a project / a tech — those are Vault notes, searched
# on demand). The prefixes are therefore behavioural only — a tech/project FACT
# belongs in a Vault note (tech/ or projects/ folder), not here, to avoid the same
# fact having two homes. Scope a preference to a project in the TEXT instead
# (e.g. "[Constraint] In the Acme project, never Mongo").
MEM0_ALLOWED_PREFIXES = ("[Preference]", "[Constraint]", "[Explicit]", "[Inferred]")

# Optional domain allowlist (comma-separated, empty = IP-filter only)
AUGMENT_ALLOWED_DOMAINS = [
    d.strip().lower() for d in os.environ.get("AUGMENT_ALLOWED_DOMAINS", "").split(",") if d.strip()
]
FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "10"))
# Sent on every external fetch. A bare "Mozilla/5.0" with no version and no Accept
# headers is a bot fingerprint that CDN filters answer with 403, which on a web
# search means arbitrary result URLs fail for no reason on our side. This is a
# complete, honest client header, not a disguise — override it to identify the
# deployment differently.
FETCH_USER_AGENT = os.environ.get(
    "FETCH_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36",
)

MAX_FILE_SIZE_BYTES = 1024 * 1024 * 2  # 2 MB Limit for DoS protection
MAX_FETCH_BYTES = 1024 * 1024 * 5      # cap external downloads at 5 MB
# How much extracted TEXT reaches the caller, in CHARACTERS — MAX_FETCH_BYTES bounds
# the download, which is a different quantity. Sized for a context window rather than
# for a disk: 5 MB of prose is roughly 1.25M tokens, so a byte-sized cap on text left
# every real truncation to the client. Raise it for a large-context model.
MAX_INGEST_CHARS = int(os.environ.get("MAX_INGEST_CHARS", "50000"))
# The largest upload_media BINARY, decoded. Its own knob rather than a multiple of
# MAX_FILE_SIZE_BYTES, which bounds note TEXT: tightening the text limit would
# otherwise silently shrink what can be uploaded. Note that base64 inflates a payload
# by 4/3 inside the JSON-RPC envelope, so MAX_REQUEST_BODY_BYTES has to leave room for
# it — at the default ingress cap the request is rejected before this limit is reached.
MAX_MEDIA_BYTES = int(os.environ.get("MAX_MEDIA_BYTES", str(1024 * 1024 * 10)))

# --- INGRESS LIMITS (DoS protection at the MCP entrypoint) ---
# Reject requests whose Content-Length exceeds this (JSON-RPC envelope + a 2 MB
# note body + base64/escaping overhead → 4 MB is a comfortable ceiling). 0 = off.
MAX_REQUEST_BODY_BYTES = int(os.environ.get("MAX_REQUEST_BODY_BYTES", str(1024 * 1024 * 4)))
# Sliding-window rate limit per client, keyed by the hashed bearer token. Applies only
# to authenticated requests — the 401 is returned before the limiter (see ratelimit.py).
RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "true").lower() in ("1", "true", "yes")
RATE_LIMIT_REQUESTS = int(os.environ.get("RATE_LIMIT_REQUESTS", "120"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))

# Expose Prometheus metrics at /metrics (behind the same auth as the MCP endpoints).
METRICS_ENABLED = os.environ.get("METRICS_ENABLED", "true").lower() in ("1", "true", "yes")

# --- MCP TRANSPORT (Streamable HTTP) ---
# The server speaks Streamable HTTP on a single /mcp route, in the session shape of
# protocol revision 2025-11-25 and earlier (ADR 13 / ADR 15).
# JSON_RESPONSE: reply with a single application/json body instead of an SSE stream
#   (simpler for non-streaming clients; default keeps the SSE stream for progress).
MCP_JSON_RESPONSE = os.environ.get("MCP_JSON_RESPONSE", "false").lower() in ("1", "true", "yes")
# STATELESS: don't retain per-session state between requests (each request is
#   self-contained). Default is stateful (per-session state retained across requests).
MCP_STATELESS = os.environ.get("MCP_STATELESS", "false").lower() in ("1", "true", "yes")
# DNS-rebinding protection (Host/Origin allowlist) inside the transport. Off by
# default: the reverse proxy is the first line of defence (see ADR 8 / README §5).
# Set a comma-separated host allowlist to enable it (e.g. "brain.example.com").
MCP_ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()
]
MCP_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()
]

# Chunking parameters for the vector index
CHUNK_SIZE_CHARS = 1500
CHUNK_OVERLAP_CHARS = 200

VAULT_ROOT = Path("vault-data").resolve()
CONTEXT_DIR = (VAULT_ROOT / "context-vault").resolve()
BRAIN_DIR = (VAULT_ROOT / "brain-vault").resolve()
# Skills are a first-class, git-persisted layer (catalog pushed, body pulled).
SKILLS_DIR = (VAULT_ROOT / "skills").resolve()
MEDIA_DIR = (VAULT_ROOT / "media").resolve()
# upload_media allowlist: only types the index understands (images natively via
# Gemini, PDFs via index_pdf_file). Everything else is rejected at upload.
ALLOWED_MEDIA_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf"}

COLLECTION_NAME = "second_brain_vault"
# Embedding model + its vector dimension (kept in sync).
# Uses the Google Gemini API to avoid a local ML/RAM footprint. The default is a
# REAL model id — an invalid id would make every embed call fail and silently
# degrade search to a no-op (clients.py probes this at startup and fails loudly).
EMBED_MODEL = os.environ.get("EMBED_MODEL", "gemini-embedding-2")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "768"))
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-3.5-flash")

# Predefined OKF/Obsidian note types (used by write_note's schema).
# ONE axis only: type == folder (where the note lives). Genre ("retro", "devlog
# entry", "ADR", "meeting") is expressed as BODY SECTIONS steered by the
# zettelkasten-discipline skill, never as a type — that kept the enum from
# collapsing two orthogonal taxonomies into one field. `identity` and `standard`
# live in the context-vault; the other ten map 1:1 to a brain-vault folder.
NOTE_TYPES = [
    "daily", "person", "company", "tech", "concept", "project",
    "research", "decision", "learning", "inbox", "identity", "standard",
]

# type == folder (Server-Enforcement B). The server DERIVES a note's folder from
# its `type_meta` so placement can't drift from the taxonomy — the singular type
# maps to its (often plural) folder name. `identity`/`standard` live flat in the
# context-vault (value None → no subfolder); the other ten are brain-vault folders.
# validate_note guarantees `type_meta` is in NOTE_TYPES before this map is read.
TYPE_FOLDER = {
    "daily": "daily",
    "person": "people",
    "company": "companies",
    "tech": "tech",
    "concept": "concepts",
    "project": "projects",
    "research": "research",
    "decision": "decisions",
    "learning": "learnings",
    "inbox": "inbox",
    "identity": None,   # context-vault, flat
    "standard": None,   # context-vault, flat
}
# Types that live in the context-vault rather than the brain-vault.
CONTEXT_TYPES = {"identity", "standard"}

# Seed skills ship WITH the repo and are copied into SKILLS_DIR on startup if absent.
SEED_SKILLS_DIR = (Path(__file__).resolve().parent / "seed-skills")

# --- VAULT ENFORCEMENT (Validating Server) ---------------------------------
# Server-side enforcement of the OKF/AI-First invariants. Each rule has a
# strength: "error" (reject the write), "warn" (allow + hint in the response),
# or "off" (skip). A profile sets the defaults; individual rules can be
# overridden via VAULT_RULE_<NAME>.
VAULT_ENFORCEMENT = os.environ.get("VAULT_ENFORCEMENT", "balanced").lower()

# Default strength per rule for each profile.
_ENFORCEMENT_PROFILES = {
    "strict": {
        "schema": "error", "preamble": "error", "sources": "warn",
        "confidence": "warn",
    },
    "balanced": {
        "schema": "error", "preamble": "warn", "sources": "warn",
        "confidence": "warn",
    },
    "lenient": {
        "schema": "error", "preamble": "off", "sources": "off",
        "confidence": "off",
    },
}



# The strengths validation.Result.emit understands. Anything else is ignored there,
# i.e. behaves like "off" — which is why an unrecognised value must never survive
# startup (see ensure_enforcement_config).
_RULE_STRENGTHS = frozenset({"error", "warn", "off"})
# Every rule a profile assigns a strength to. The profiles are exhaustive and agree
# on the rule set, so any one of them names the full set.
_RULE_NAMES = frozenset(_ENFORCEMENT_PROFILES["balanced"])


def rule_strength(rule: str) -> str:
    """Resolve a rule's strength — per-rule ENV override beats the profile.

    The override is returned as configured; ensure_enforcement_config has already
    guaranteed it is one of _RULE_STRENGTHS, so no fallback is needed here.
    """
    override = os.environ.get(f"VAULT_RULE_{rule.upper()}")
    if override:
        return override.lower()
    profile = _ENFORCEMENT_PROFILES[VAULT_ENFORCEMENT]
    return profile.get(rule, "off")


def ensure_enforcement_config() -> None:
    """Fail-closed — refuse an enforcement configuration that would silently not apply.

    `schema` is the server's only hard invariant: it is what keeps `type_meta` inside
    NOTE_TYPES, and because the folder is DERIVED from the type, a note with an
    unchecked type is also filed flat instead of into its type folder. Yet a
    misconfiguration disables it without a trace, in two distinct ways:

    - a bad STRENGTH (``VAULT_RULE_SCHEMA=eror``) reaches Result.emit, which handles
      only "error" and "warn" and drops everything else — the rule stops rejecting.
    - a bad RULE NAME (``VAULT_RULE_SCHMEA=off``) matches no rule at all, so the
      variable does nothing while the operator believes it took effect.

    Neither is distinguishable from a working setup at runtime, so both are refused
    here rather than logged and stepped over.
    """
    if VAULT_ENFORCEMENT not in _ENFORCEMENT_PROFILES:
        raise SystemExit(
            f"FATAL: VAULT_ENFORCEMENT='{VAULT_ENFORCEMENT}' is not a known profile. "
            f"Use one of: {', '.join(sorted(_ENFORCEMENT_PROFILES))}."
        )
    for var, value in sorted(os.environ.items()):
        if not var.startswith("VAULT_RULE_"):
            continue
        rule = var[len("VAULT_RULE_"):].lower()
        if rule not in _RULE_NAMES:
            raise SystemExit(
                f"FATAL: {var} does not name a validation rule, so it has no effect. "
                f"Known rules: {', '.join(sorted(_RULE_NAMES))}."
            )
        if value.lower() not in _RULE_STRENGTHS:
            raise SystemExit(
                f"FATAL: {var}='{value}' is not a valid rule strength. "
                f"Use one of: {', '.join(sorted(_RULE_STRENGTHS))}."
            )


# Mechanical wikilink behaviour against EXISTING notes — off | warn | auto.
VAULT_AUTOLINK = os.environ.get("VAULT_AUTOLINK", "warn").lower()

# Dedup thresholds (cosine similarity). Soft = report similar note;
# hard = reject as near-duplicate. Hard is opt-in (set <= 1.0 to enable).
VAULT_DEDUP_SOFT = float(os.environ.get("VAULT_DEDUP_SOFT", "0.92"))
VAULT_DEDUP_HARD = float(os.environ.get("VAULT_DEDUP_HARD", "2.0"))  # >1 = disabled

# The canonical OKF frontmatter — the server OWNS this, clients cannot forge it.
# Required structural section every note must contain.
OKF_PREAMBLE_HEADING = "## AI Summary"


# Placeholders this project ships itself (.env.example, README, INSTALL), plus the
# classic weak choices. Copying .env.example and forgetting to edit it is the
# likeliest misconfiguration, and it would put a network-facing server behind a
# token that is public in every clone of this repo. Matched case-insensitively.
_REJECTED_TOKENS = frozenset({
    "default-token", "your-secure-bearer-token", "your_auth_token",
    "a_very_long_secure_password", "changeme", "change-me", "secret",
    "password", "token", "test", "test-token",
})

# Long enough that guessing is hopeless; `openssl rand -hex 32` yields 64.
MIN_AUTH_TOKEN_LEN = 16


def ensure_auth_token() -> str:
    """Fail-closed — refuse to start unless AUTH_TOKEN is a real secret.

    Each rejection names what is wrong, because the operator has to fix it before
    the server will come up at all.

    Tests / offline tooling may import the package without a token; we detect
    pytest and fall back to a dummy token only in that case.
    """
    global AUTH_TOKEN
    if not AUTH_TOKEN:
        if "pytest" in sys.modules:
            AUTH_TOKEN = "test-token"
            return AUTH_TOKEN
        raise SystemExit(
            "FATAL: AUTH_TOKEN is not set. Generate one with `openssl rand -hex 32` "
            "and set it in the environment before starting the server."
        )
    # Compare a trimmed copy — a value pasted into .env can carry stray
    # whitespace — but never rewrite the operator's actual secret.
    probe = AUTH_TOKEN.strip()
    if probe.lower() in _REJECTED_TOKENS:
        raise SystemExit(
            "FATAL: AUTH_TOKEN is a placeholder from .env.example or the docs, so it "
            "is public in every clone of this repo. Generate a real one with "
            "`openssl rand -hex 32`."
        )
    if not probe.isascii():
        # server._token_matches compares via secrets.compare_digest, which raises
        # TypeError on non-ASCII and is caught there as a plain mismatch. A
        # non-ASCII token would therefore reject EVERY request, including the
        # correct one, with nothing in the logs to explain it. Refusing to start
        # is the louder failure.
        raise SystemExit(
            "FATAL: AUTH_TOKEN must be ASCII. A non-ASCII token cannot be compared "
            "by the bearer check and would reject every request, including yours."
        )
    if len(probe) < MIN_AUTH_TOKEN_LEN:
        raise SystemExit(
            f"FATAL: AUTH_TOKEN is too short ({len(probe)} chars, minimum "
            f"{MIN_AUTH_TOKEN_LEN}). Generate one with `openssl rand -hex 32`."
        )
    return AUTH_TOKEN


def ensure_git_repo_url() -> str | None:
    """Fail-closed — refuse a GIT_REPO_URL that carries a credential.

    GitPython masks the credential in the COMMAND LINE it puts on a
    GitCommandError, but git's own stderr is appended unmasked, and the JSON
    formatter writes that straight out: one failed clone against an
    ``https://<token>@host`` remote and the token sits in the log in plaintext.
    Rejecting the URL removes the credential from the process instead of trying to
    scrub every path it could reach.

    ADR 4 authenticates via a mounted SSH deploy key, so no supported setup needs
    userinfo: ``git@host:path``, ``ssh://git@host/path`` and a bare
    ``https://host/path`` (git credential helper) all pass. For http(s) any
    userinfo is a credential; for ssh the username is an identity (``git@``) and
    only a password is one.
    """
    if not GIT_REPO_URL:
        return GIT_REPO_URL
    try:
        parsed = urlsplit(GIT_REPO_URL)
        has_credential = (bool(parsed.username)
                          if parsed.scheme.lower() in ("http", "https")
                          else bool(parsed.password))
    except ValueError:
        # An unparseable authority (a malformed IPv6 literal makes urlsplit itself
        # raise) — refuse rather than let a URL we could not inspect through.
        raise SystemExit(
            "FATAL: GIT_REPO_URL could not be parsed. Use an SSH remote such as "
            "git@github.com:you/your-vault.git (see .env.example)."
        )
    if has_credential:
        raise SystemExit(
            "FATAL: GIT_REPO_URL embeds a credential. git prints the URL in its own "
            "error output, which would put that secret in the logs verbatim. "
            "Authenticate with the mounted SSH deploy key instead — see .env.example "
            "— e.g. git@github.com:you/your-vault.git. Revoke the token you just "
            "configured; it has been on this host's command line."
        )
    return GIT_REPO_URL


# Resolve the token at import time so a misconfigured server fails fast.
ensure_auth_token()
ensure_git_repo_url()
ensure_enforcement_config()
