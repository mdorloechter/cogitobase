"""Containment: a path, a name or a URL must not reach outside what it may touch.

Symlink escapes, traversal, glob patterns treated as names, the size cap, SSRF and
its redirect bypass, and the point-id collision that would let one vault overwrite
another's vectors.
"""
import pytest
import config
import security
from security import find_notes, point_id_for, validate_external_url
from conftest import _link_outside, call


# ----------------- Vault containment: symlinks must not escape -----------------
# The vault is a git working tree (ADR 4), so a `120000` blob committed to the
# mirror materialises as a link on the next clone/pull — planting one needs commit
# access to the mirror, not shell access to the host. git_sync sets
# core.symlinks=false to stop that, and every content reader guards itself as well;
# these tests cover the readers.
def test_find_notes_rejects_symlink_escaping_the_vault(tmp_path):
    """The bare-basename fallback must not return a link pointing out of the vault.

    The explicit-path branch is covered by validate_safe_path; this is the branch
    that reaches a file by glob, where is_file() alone would follow the link.
    """
    _link_outside(tmp_path, config.BRAIN_DIR / "standard" / "onboarding.md")
    assert find_notes("onboarding.md") == []


@pytest.mark.asyncio
async def test_read_note_does_not_serve_a_symlinked_host_file(tmp_path):
    """End-to-end: the tool reports a miss and the host file's content never
    reaches the caller."""
    _link_outside(tmp_path, config.BRAIN_DIR / "standard" / "onboarding.md")
    res = await call("read_note", {"filename": "onboarding.md"})
    assert "HOSTFILE-SECRET-CONTENT" not in res[0].text
    assert "not found" in res[0].text.lower()


@pytest.mark.asyncio
async def test_read_note_skips_a_symlink_and_keeps_looking(tmp_path):
    """Control: a rejected candidate must not end the search.

    Both files match the basename, so the fallback has to skip the link and carry
    on to the genuine note instead of returning the link or giving up at the first
    hit. Neither is at the literal path, so this exercises the glob branch only.
    """
    _link_outside(tmp_path, config.BRAIN_DIR / "standard" / "onboarding.md")
    real = config.BRAIN_DIR / "tech" / "onboarding.md"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("the real note body", encoding="utf-8")
    res = await call("read_note", {"filename": "onboarding.md"})
    assert "the real note body" in res[0].text
    assert "HOSTFILE-SECRET-CONTENT" not in res[0].text


# ----------------- Lookup: a filename is a name, not a glob pattern -----------------
_SECRET_BODY = "CONFIDENTIAL-SALARY-FIGURE"


def _two_note_vault():
    """A public note plus one the caller is not supposed to reach by guessing.
    Neither sits at a literal path, so lookups go through the basename fallback."""
    public = config.BRAIN_DIR / "concepts" / "public-note.md"
    secret = config.BRAIN_DIR / "people" / "secret-salary.md"
    for p, body in ((public, "harmless public content"), (secret, _SECRET_BODY)):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# Note\n{body}", encoding="utf-8")
    return public, secret


# Literal patterns, not derived from the code under test: a list built from a
# production constant could empty out and collapse the parametrize into a skip.
_GLOB_PATTERNS = ["*", "*.md", "secret*", "?ecret-salary", "[sp]*", "**/*"]


@pytest.mark.parametrize("pattern", _GLOB_PATTERNS)
@pytest.mark.asyncio
async def test_read_note_treats_a_glob_pattern_as_a_miss(pattern):
    """rglob reads its argument as a pattern, so an unescaped filename lets a caller
    read a note whose name it never knew — the vault's brain/context separation
    reduced to a guessing game. A pattern must find nothing."""
    _two_note_vault()
    assert find_notes(pattern) == []
    res = await call("read_note", {"filename": pattern})
    assert _SECRET_BODY not in res[0].text
    assert "not found" in res[0].text.lower()


@pytest.mark.asyncio
async def test_glob_pattern_cannot_delete_or_rename_someone_elses_note():
    """The mutating tools resolve through the same lookup, so an unescaped pattern
    means data loss on request: delete_note('secret*') would remove a note the
    caller cannot name. Both must miss, and the file must survive."""
    _, secret = _two_note_vault()

    res = await call("delete_note", {"filename": "secret*"})
    assert "not found" in res[0].text.lower()
    assert secret.exists(), "a pattern must not delete a note"

    res = await call("rename_note", {"old_filename": "secret*", "new_filename": "grabbed.md"})
    assert "not found" in res[0].text.lower()
    assert secret.exists()
    assert not (config.BRAIN_DIR / "people" / "grabbed.md").exists()


@pytest.mark.asyncio
async def test_a_note_whose_name_contains_a_metacharacter_stays_readable():
    """write_note accepts glob metacharacters in a filename, so escaping must
    search for the literal name rather than blocking the character — otherwise
    this note would become unreachable, and it must return its OWN body, not
    another note's.

    Brackets rather than '*': both are glob metacharacters, but Windows rejects
    '*' in a filename outright, so a star-named note cannot even be created there.
    """
    _two_note_vault()
    await call("write_note", {
        "vault": "brain", "filename": "brack[et]note", "title": "Bracket",
        "type_meta": "concept", "tags": [],
        "content": "## AI Summary\nSummary line.\n\nBRACKET-NOTE-OWN-BODY"})
    assert (config.BRAIN_DIR / "concepts" / "brack[et]note.md").exists()

    res = await call("read_note", {"filename": "brack[et]note"})
    assert "BRACKET-NOTE-OWN-BODY" in res[0].text
    assert _SECRET_BODY not in res[0].text


# ----------------- traversal consistent across read/write -----------------
@pytest.mark.asyncio
async def test_path_traversal_protection_read():
    with pytest.raises(ValueError, match="Security Error"):
        await call("read_note", {"filename": "../../../../etc/passwd"})


@pytest.mark.asyncio
async def test_path_traversal_protection_write():
    with pytest.raises(ValueError, match="Security Error"):
        await call("write_note", {
            "vault": "brain", "filename": "../../evil", "title": "x",
            "type_meta": "concept", "tags": [], "content": "x",
        })


# ----------------- DoS limit -----------------
@pytest.mark.asyncio
async def test_max_file_size_protection(monkeypatch):
    monkeypatch.setattr(config, "MAX_FILE_SIZE_BYTES", 10)
    res = await call("write_note", {
        "vault": "brain", "filename": "large_file", "title": "Large",
        "type_meta": "daily", "tags": [], "content": "This string is way longer than 10 bytes.",
    })
    assert "over the" in res[0].text and "MAX_FILE_SIZE_BYTES" in res[0].text


# ----------------- path-based point ids don't collide -----------------
def test_point_id_distinct_across_vaults():
    brain = config.BRAIN_DIR / "x.md"
    context = config.CONTEXT_DIR / "x.md"
    assert point_id_for(brain) != point_id_for(context)
    assert point_id_for(brain, 0) != point_id_for(brain, 1)


# ----------------- SSRF validator -----------------
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin",
    "http://localhost/",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "http://100.64.1.1/",              # RFC 6598 CGNAT — must be blocked too
    "http://[::ffff:100.64.1.1]/",     # IPv4-mapped IPv6 CGNAT — must be blocked too
    "file:///etc/passwd",
    "ftp://example.com/",
])
def test_ssrf_blocks_internal(url):
    with pytest.raises(ValueError, match="Security Error"):
        validate_external_url(url)


def test_ssrf_allows_public(monkeypatch):
    monkeypatch.setattr(security.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert validate_external_url("https://example.com/page") == "https://example.com/page"


def test_ssrf_allowlist(monkeypatch):
    monkeypatch.setattr(config, "AUGMENT_ALLOWED_DOMAINS", ["example.com"])
    monkeypatch.setattr(security.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert validate_external_url("https://sub.example.com/x")
    with pytest.raises(ValueError, match="Security Error"):
        validate_external_url("https://evil.com/x")


# ----------------- SSRF redirect bypass is blocked -----------------
def test_safe_fetch_revalidates_redirect_target(monkeypatch):
    """A redirect to an internal host must be re-validated and blocked,
    even when the initial URL resolves to a public IP."""
    import urllib.error

    # First hop = public, redirect target = loopback.
    def fake_getaddrinfo(host, *a, **k):
        if host == "evil-redirect.com":
            return [(2, 1, 6, "", ("93.184.216.34", 0))]
        return [(2, 1, 6, "", ("127.0.0.1", 0))]  # internal-redirect.com → loopback
    monkeypatch.setattr(security.socket, "getaddrinfo", fake_getaddrinfo)

    class FakeOpener:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 302, "Found",
                {"Location": "http://internal-redirect.com/secret"}, None)
    monkeypatch.setattr(security.urllib.request, "build_opener", lambda *a: FakeOpener())

    with pytest.raises(ValueError, match="Security Error"):
        security.safe_fetch("http://evil-redirect.com/start")


def test_safe_fetch_enforces_size_limit(monkeypatch):
    monkeypatch.setattr(config, "MAX_FETCH_BYTES", 10)
    monkeypatch.setattr(security.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])

    class FakeResp:
        def read(self, n): return b"x" * n  # always returns more than asked → over limit
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class FakeOpener:
        def open(self, req, timeout=None): return FakeResp()
    monkeypatch.setattr(security.urllib.request, "build_opener", lambda *a: FakeOpener())

    with pytest.raises(ValueError, match="size limit"):
        security.safe_fetch("http://example.com/big")


# ----------------- safe_fetch pins the validated IP (no rebinding) -----------------
def test_safe_fetch_pins_validated_ip(monkeypatch):
    """The IP we validated must be the IP we connect to — a second resolution that
    returns an internal address must not be able to redirect the socket."""
    seen = {"connect_ip": None}

    monkeypatch.setattr(security.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])

    class FakeResp:
        def read(self, n): return b"<html>ok</html>"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class FakeOpener:
        def open(self, req, timeout=None): return FakeResp()

    def fake_pinned_opener(ip):
        seen["connect_ip"] = ip
        return FakeOpener()

    monkeypatch.setattr(security, "_build_pinned_opener", fake_pinned_opener)
    raw = security.safe_fetch("http://example.com/page")
    assert raw == b"<html>ok</html>"
    # Connected to the validated public IP, not a re-resolved host.
    assert seen["connect_ip"] == "93.184.216.34"


# ----------------- outbound request headers -----------------
def _capture_fetch_request(monkeypatch):
    """Run safe_fetch against a stub opener and hand back the Request it was given."""
    captured = {}

    monkeypatch.setattr(security.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])

    class FakeResp:
        def read(self, n): return b"<html>ok</html>"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class FakeOpener:
        def open(self, req, timeout=None):
            captured["req"] = req
            return FakeResp()

    monkeypatch.setattr(security, "_build_pinned_opener", lambda ip: FakeOpener())
    security.safe_fetch("http://example.com/page")
    return captured["req"]


def test_safe_fetch_sends_a_complete_browser_header_set(monkeypatch):
    """A bare UA with no version and no Accept headers is the fingerprint CDN bot
    filters answer with 403 — on a web search that fails arbitrary result URLs for
    a reason that is ours, not the target's."""
    req = _capture_fetch_request(monkeypatch)
    ua = req.get_header("User-agent")
    assert ua == config.FETCH_USER_AGENT
    assert ua != "Mozilla/5.0", "a version-less UA is the bot fingerprint we are avoiding"
    assert "/" in ua.split("Mozilla/5.0", 1)[1], "UA carries no product version"
    # Accept and Accept-Language are half of the fingerprint; a UA alone still reads
    # as a scraper.
    assert "text/html" in req.get_header("Accept")
    assert req.get_header("Accept-language")


def test_fetch_user_agent_is_operator_overridable(monkeypatch):
    """The header identifies the deployment, so the operator has to be able to set it
    (e.g. to a contactable string) without patching the source."""
    monkeypatch.setattr(config, "FETCH_USER_AGENT", "cogitobase/1.0 (+https://example.org)")
    req = _capture_fetch_request(monkeypatch)
    assert req.get_header("User-agent") == "cogitobase/1.0 (+https://example.org)"
