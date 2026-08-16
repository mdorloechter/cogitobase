"""Security & low-level helpers: path validation, SSRF filtering, point ids, chunking.

Pure functions with no I/O side effects beyond DNS resolution, so they are
directly unit-testable. They reference ``config`` attributes at call-time so
tests can monkeypatch ``config.VAULT_ROOT`` etc.
"""
import glob
import hashlib
import http.client
import ipaddress
import math
import socket
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlparse, urljoin

import config


def validate_safe_path(directory: Path, filename: str) -> Path:
    """Resolve `filename` under `directory`, blocking path traversal."""
    safe_path = (directory / filename).resolve()
    if not safe_path.is_relative_to(directory):
        raise ValueError("Security Error: Path traversal blocked")
    return safe_path


def is_contained_file(path: Path, directory: Path) -> bool:
    """True if `path` is a real file that stays inside `directory`.

    For every glob/rglob hit whose CONTENT is then read or indexed. ``is_file()``
    alone is not enough: it follows symlinks, so a link planted in the vault
    resolves to a target outside it. The vault is a git working tree (ADR 4), and
    git materialises a symlink from a ``120000`` blob on clone/pull — so planting
    one needs only commit access to the mirror, not shell access to the host.

    Rejects the link itself, then re-checks the resolved path so a link chain or a
    future mount trick cannot slip past either.
    """
    if path.is_symlink() or not path.is_file():
        return False
    try:
        return path.resolve().is_relative_to(directory.resolve())
    except OSError:
        # Broken link or unreadable parent — treat as out of bounds.
        return False


def _vault_dirs() -> list[tuple[str, Path]]:
    """The searchable note vaults, as (name, directory) in lookup order.

    The name is the vault's own directory name, so a candidate path printed with
    it (``brain-vault/projects/roadmap.md``) is exactly what the vault-qualified
    branch of find_notes resolves back.
    """
    return [(d.name, d) for d in (config.BRAIN_DIR, config.CONTEXT_DIR)]


def find_notes(filename: str) -> list[Path]:
    """Locate EVERY note matching `filename`, across both vaults.

    Three ways to name a note, dispatched on the SHAPE of the name — exclusively,
    never as a fallback chain: a name that hits one branch must not also collect
    hits from another, or a single literal match would mask the same-named notes
    the other branches see and pass for unique.

    1. Vault-qualified path (``brain-vault/projects/roadmap.md``) — resolves in
       that one vault only, so it can always name exactly one note. This is the
       form the callers echo back as disambiguation candidates, and the only form
       that stays unambiguous when brain and context share a relative path.
    2. Vault-relative path (``projects/roadmap.md``) — that exact path in each vault.
    3. Bare basename (``roadmap.md``) — recursive search, because
       Server-Enforcement B derives a note's folder from its type, so a caller
       that only knows the name must still reach it wherever placement put it.

    Returns ALL matches, never a pick among them: the folder encodes a note's type,
    so same-named notes are distinct notes, and choosing between them by directory
    order would send append/rename/delete at an arbitrary one. Callers refuse an
    ambiguous name instead. An empty list is 'not found'; a security violation
    raises ValueError, so traversal attempts surface consistently with write_note.
    """
    if not filename.endswith(".md"):
        filename += ".md"
    parts = Path(filename).parts
    matches = []

    if len(parts) > 1:
        for name, d in _vault_dirs():
            if parts[0] == name:
                # Lets validate_safe_path's ValueError propagate: a traversal
                # attempt under an explicit vault is not a miss to fall through on.
                filepath = validate_safe_path(d, str(Path(*parts[1:])))
                return [filepath] if is_contained_file(filepath, d) else []

        # A relative path names one location per vault, so no recursive search.
        security_error: ValueError | None = None
        for _, d in _vault_dirs():
            try:
                filepath = validate_safe_path(d, filename)
            except ValueError as e:
                security_error = e
                continue
            if is_contained_file(filepath, d):
                matches.append(filepath)
        if not matches and security_error is not None:
            # Rejected by every vault → a traversal attempt, not a miss.
            raise security_error
        return sorted(matches)

    # Bare basename: search the type-subfolders. rglob's `**` matches zero
    # directories too, so this also finds a note sitting flat in the vault root.
    # The search string has no directory part, so it cannot escape a vault; the
    # HIT still can, hence is_contained_file on every candidate (the path branches
    # above get that check from validate_safe_path).
    #
    # glob.escape, because rglob reads its argument as a PATTERN: a caller passing
    # "*" or "[sp]*" would otherwise reach a note whose name it never knew, and
    # delete_note/rename_note would act on whatever the pattern matched. Escaping
    # searches for the literal name instead, which also keeps notes whose names
    # legitimately contain a metacharacter (write_note allows them) findable.
    for _, d in _vault_dirs():
        if d.exists():
            for f in d.rglob(glob.escape(filename)):
                if is_contained_file(f, d):
                    matches.append(f)
    # Sorted, so the candidate list a caller sees does not depend on filesystem
    # iteration order.
    return sorted(matches)


def vault_qualified_path(filepath: Path) -> str:
    """The note's name as a caller can send it back: ``<vault>/<relative path>``.

    Unlike vault_relative_path (the index's identity key, rooted at VAULT_ROOT),
    this is rooted so find_notes' vault-qualified branch accepts it verbatim.
    Falls back to the basename for a path outside the note vaults.
    """
    for _, d in _vault_dirs():
        try:
            return (Path(d.name) / filepath.resolve().relative_to(d.resolve())).as_posix()
        except (ValueError, OSError):
            continue
    return filepath.name


def vault_relative_path(filepath: Path) -> str:
    """The note's identity inside the vault: its path relative to VAULT_ROOT.

    Two notes can share a basename (a concept and a project both called
    roadmap.md), so this — not the basename — is what identifies a note's points
    in the index. Falls back to the basename for a path outside the vault, which
    only happens for a caller passing something the vault never stored.
    """
    try:
        return filepath.resolve().relative_to(config.VAULT_ROOT).as_posix()
    except ValueError:
        return filepath.name


def point_id_for(filepath: Path, chunk_idx: int = 0) -> int:
    """Derive the Qdrant point id from the vault-relative path (+ chunk index),
    so identically named notes in different vaults never collide."""
    key = f"{vault_relative_path(filepath)}#{chunk_idx}"
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % (10 ** 15)


_FENCE = "```"


def _blocks_with_context(text: str) -> list[tuple[str, str]]:
    """Split a document into (block, heading context) pairs at blank lines.

    A block is a paragraph, a list, or a fenced code block — the fence is kept whole
    however many blank lines it holds, so no chunk boundary lands inside it. Lists
    survive because they carry no blank line of their own.

    The context is the H1 plus the innermost sub-heading in force where the block
    starts. A heading is picked up wherever it appears, but takes effect only for the
    blocks that follow it, so a chunk opening on the heading itself does not get that
    same line prepended twice.
    """
    blocks: list[tuple[str, str]] = []
    lines: list[str] = []
    h1 = section = context = ""
    in_fence = False

    for line in text.split("\n"):
        stripped = line.strip()
        if not in_fence and not stripped:
            if lines:
                blocks.append(("\n".join(lines), context))
                lines = []
            continue
        if not lines:
            context = "\n".join(h for h in (h1, section) if h)
        if not in_fence:
            if stripped.startswith("# ") and not h1:
                h1 = stripped
            elif stripped.startswith("##"):
                section = stripped
        if stripped.startswith(_FENCE):
            in_fence = not in_fence
        lines.append(line)
    if lines:
        blocks.append(("\n".join(lines), context))
    return blocks


def _split_oversized(block: str, limit: int) -> list[str]:
    """Cut a block that cannot fit into a chunk on its own, sentence ends first.

    A single paragraph longer than the chunk size would otherwise push the chunk past
    the limit, and the limit is what keeps an embedding request within the model's
    input window.

    The pieces are evenly sized rather than filled to the brim, because filling leaves
    the remainder as the last piece: a 1501-character paragraph against a 1500 limit
    would end in a 1-character chunk that costs an embedding call and a point while
    saying nothing.
    """
    if len(block) <= limit:
        return [block]
    pieces, rest = [], block
    while len(rest) > limit:
        wanted = math.ceil(len(rest) / limit)
        target = math.ceil(len(rest) / wanted)
        window = rest[:target]
        cut = max(window.rfind(". "), window.rfind("! "),
                  window.rfind("? "), window.rfind("\n"))
        # A cut in the first half would leave a fragment too small to carry meaning;
        # take the whole window instead and accept the mid-sentence edge.
        cut = cut + 1 if cut > target // 2 else target
        piece, rest = rest[:cut].strip(), rest[cut:].strip()
        if piece:
            pieces.append(piece)
    if rest:
        pieces.append(rest)
    return pieces


def _assemble(context: str, blocks: list[str], overlap: str) -> str:
    """Render one chunk: heading context, the block carried over, then the blocks."""
    parts = [p for p in (context, overlap) if p]
    parts.extend(blocks)
    return "\n\n".join(parts)


def chunk_text(text: str) -> list[str]:
    """Split a document into chunks that end where its blocks do.

    Cuts fall on blank lines, so a chunk holds whole paragraphs, list items and code
    fences rather than whatever a character window happened to span. Each chunk is
    prefixed with the headings in force at its start, because search renders the
    BEGINNING of the chunk it matched (`_render_hit`): without them the caller reads
    prose with no indication which note or which section it came from.

    Every chunk carries at least one block that the previous one does not, so none is
    a pure repeat of its predecessor's tail. The overlap that keeps a hit near a
    boundary findable is one whole block, and it is dropped when it would push the
    chunk over the size limit.
    """
    if len(text) <= config.CHUNK_SIZE_CHARS:
        return [text]
    blocks = _blocks_with_context(text)
    if not blocks:
        return [text]

    sized = []
    for block, context in blocks:
        limit = config.CHUNK_SIZE_CHARS - (len(context) + 2 if context else 0)
        for piece in _split_oversized(block, max(limit, 1)):
            sized.append((piece, context))

    chunks, pending, pending_context, overlap = [], [], "", ""
    idx = 0
    while idx < len(sized):
        block, context = sized[idx]
        if not pending:
            pending_context = context
            # Drop a carried-over block that is too big to be an overlap, that would
            # push this chunk over the limit, or that the heading context already
            # repeats — the last happens whenever a chunk boundary falls right after
            # a heading, which stands alone as a block of its own.
            if overlap and (len(overlap) > config.CHUNK_OVERLAP_CHARS
                            or overlap in context
                            or len(_assemble(context, [block], overlap))
                            > config.CHUNK_SIZE_CHARS):
                overlap = ""
            pending = [block]
            idx += 1
            continue
        trial = pending + [block]
        if len(_assemble(pending_context, trial, overlap)) <= config.CHUNK_SIZE_CHARS:
            pending = trial
            idx += 1
            continue
        chunks.append(_assemble(pending_context, pending, overlap))
        overlap, pending = pending[-1], []
    chunks.append(_assemble(pending_context, pending, overlap))
    return chunks


# RFC 6598 Carrier-Grade NAT range — not covered by ipaddress.is_private, but
# routable-looking and reachable inside many hosting environments, so block it too.
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def _is_blocked_ip(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str)
    v4 = ip if ip.version == 4 else ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
            or (v4 is not None and v4 in _CGNAT_NET))


def resolve_validated_ips(url: str) -> list[str]:
    """Validate scheme/allowlist, resolve DNS once, and return the validated IPs.

    Raises ValueError on any policy violation. The returned IPs are the ones that
    passed the private/internal check — callers should connect to one of THESE
    (IP pinning) rather than re-resolving the host, which would reopen a DNS
    rebinding window between the check and the connect (TOCTOU).
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Security Error: only http/https URLs are allowed")
    host = parsed.hostname
    if not host:
        raise ValueError("Security Error: missing host in URL")
    if config.AUGMENT_ALLOWED_DOMAINS and not any(
        host.lower() == d or host.lower().endswith("." + d) for d in config.AUGMENT_ALLOWED_DOMAINS
    ):
        raise ValueError("Security Error: host not in AUGMENT_ALLOWED_DOMAINS")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError("Security Error: host could not be resolved")
    ips = [info[4][0] for info in infos]
    for ip_str in ips:
        if _is_blocked_ip(ip_str):
            raise ValueError("Security Error: private/internal target blocked")
    return ips


def validate_external_url(url: str) -> str:
    """Block SSRF — only http(s), resolve DNS and reject private/internal targets."""
    resolve_validated_ips(url)
    return url


def _build_pinned_opener(pinned_ip: str):
    """Build a urllib opener that connects to ``pinned_ip`` while keeping the original
    hostname for the Host header and TLS SNI/cert validation.

    Closes the residual DNS-rebinding window — the IP we validated is the
    exact IP we connect to, so a host that re-resolves to an internal address
    between check and connect cannot redirect the socket.
    """
    class _PinnedHTTPConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.create_connection((pinned_ip, self.port), self.timeout)
            if self._tunnel_host:
                self._tunnel()

    class _PinnedHTTPSConnection(http.client.HTTPSConnection):
        def connect(self):
            sock = socket.create_connection((pinned_ip, self.port), self.timeout)
            if self._tunnel_host:
                self.sock = sock
                self._tunnel()
                sock = self.sock
            # server_hostname keeps SNI + certificate validation on the real host.
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)

    class _PinnedHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(_PinnedHTTPConnection, req)

    class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(_PinnedHTTPSConnection, req)

    return urllib.request.build_opener(_NoRedirect, _PinnedHTTPHandler(), _PinnedHTTPSHandler())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not follow redirects automatically — every hop must be re-validated."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_headers() -> dict[str, str]:
    """Request headers for every external fetch.

    A UA alone is not enough: a request with no Accept and no Accept-Language looks
    like a scraper to a CDN bot filter, which answers 403. Read from config on every
    call so the operator's FETCH_USER_AGENT applies without a reimport.
    """
    return {
        "User-Agent": config.FETCH_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
    }


def safe_fetch(url: str, max_bytes: int | None = None, timeout: int | None = None) -> bytes:
    """SSRF-safe fetch with manual, re-validated redirects and a hard size cap.

    Closes the redirect/DNS-rebinding bypass in validate_external_url: each hop is
    validated again before it is followed, and the body is read with a byte limit.
    """
    if max_bytes is None:
        max_bytes = config.MAX_FETCH_BYTES
    if timeout is None:
        timeout = config.FETCH_TIMEOUT

    current = url
    for _ in range(5):  # bounded redirect chain
        # Resolve + validate EVERY hop, then connect to the exact validated IP.
        # Pinning the IP closes the DNS-rebinding window (TOCTOU) that re-resolving
        # the hostname at connect time would leave open.
        validated_ips = resolve_validated_ips(current)
        opener = _build_pinned_opener(validated_ips[0])
        req = urllib.request.Request(current, headers=_fetch_headers())
        try:
            resp = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                location = e.headers.get("Location")
                if not location:
                    raise ValueError("Security Error: redirect without Location")
                current = urljoin(current, location)
                continue
            raise
        with resp:
            raw = resp.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError("Security Error: response exceeds size limit (DoS protection)")
        return raw
    raise ValueError("Security Error: too many redirects")
