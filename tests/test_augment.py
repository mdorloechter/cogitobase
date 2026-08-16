"""Handler tests for the augmentation tools (search_web, ingest_media,
analyze_github_repo). These exercise the tool logic itself — SSRF handling,
content extraction, and honest reporting of what was cut — with the network
mocked, covering the tools with the largest external attack surface.
"""
import sys
import types
import urllib.error

import pytest

import config
import registry
import security
import augment  # noqa: F401  (registers the handlers)


def call(name, args=None):
    return registry.dispatch(name, args or {})


def stub_dns(monkeypatch, ip="93.184.216.34"):
    """Resolve every host to one public IP, so the SSRF filter runs its real logic
    (scheme, allowlist, private-range check) without touching the network."""
    monkeypatch.setattr(security.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", (ip, 0))])


def _long(marker):
    """A stub extract long enough to clear the boilerplate threshold.

    An extract shorter than that is navigation chrome, which the handler drops on
    purpose — so a test asserting on a marker has to hand back a realistic body,
    not just the marker.
    """
    return marker + " " + "lorem ipsum dolor sit amet " * 20


# ----------------- the tool's name and what it promises -----------------
def test_the_web_search_tool_is_named_and_described_as_one_round():
    """"deep" would promise an agentic loop this tool does not run: one round, no
    link following, no refinement — by design, because ADR 7 keeps that loop in the
    client. The name also lines up with search_vault/search_memories, the other two
    search surfaces."""
    names = {t.name for t in registry.all_tools()}
    assert "search_web" in names
    assert "deep_research" not in names, "the retired name is registered again"
    spec = next(t for t in registry.all_tools() if t.name == "search_web")
    assert "deep" not in spec.description.lower(), \
        f"the description promises depth again: {spec.description!r}"


def test_no_doc_or_skill_still_names_the_retired_tool():
    """A doc or seed skill naming a tool the registry does not have sends the client
    at a name that errors — and the seed skill is what steers it there."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    stale = []
    for rel in ("README.md", "ARCHITECTURE.md", "PRIVACY.md", "SECURITY.md",
                "skills.py", "seed-skills/research-loop.md"):
        if "deep_research" in (root / rel).read_text(encoding="utf-8"):
            stale.append(rel)
    assert not stale, f"these still name deep_research: {stale}"


# ----------------- search_web -----------------
@pytest.mark.asyncio
async def test_search_web_extracts_and_skips_blocked(monkeypatch):
    """A public result is extracted; a result whose URL is blocked by the SSRF
    policy is skipped with a notice — not fatal to the whole search."""
    fake_ddgs = types.SimpleNamespace()

    class FakeDDGS:
        def text(self, query, max_results=3):
            return [
                {"title": "Good", "href": "https://good.example/a", "body": "snippet a"},
                {"title": "Bad", "href": "http://169.254.169.254/meta", "body": "snippet b"},
            ]

    monkeypatch.setitem(sys.modules, "ddgs",
                        types.SimpleNamespace(DDGS=FakeDDGS))
    fake_trafilatura = types.SimpleNamespace(extract=lambda raw: _long("EXTRACTED BODY"))
    monkeypatch.setitem(sys.modules, "trafilatura", fake_trafilatura)

    def fake_fetch(url, *a, **k):
        if "169.254" in url:
            raise ValueError("Security Error: private/internal target blocked")
        return b"<html>x</html>"
    monkeypatch.setattr(augment, "safe_fetch", fake_fetch)

    res = await call("search_web", {"query": "anything"})
    out = res[0].text
    assert "EXTRACTED BODY" in out                 # public result was extracted
    assert "skipped — URL blocked by SSRF policy" in out  # blocked result handled, not fatal


def _ddgs_returning(monkeypatch, results):
    class FakeDDGS:
        def text(self, query, max_results=3):
            return results
    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=FakeDDGS))


# A bot filter answering a search result with 403 is routine, and urllib raises
# HTTPError for it — an OSError, not a ValueError. The middle result is the failing
# one on purpose: the result AFTER it proves the loop continued rather than aborting.
@pytest.mark.asyncio
@pytest.mark.parametrize("exc,expected", [
    (urllib.error.HTTPError("https://blocked.example/b", 403, "Forbidden", {}, None),
     "skipped — HTTP 403"),
    (urllib.error.HTTPError("https://blocked.example/b", 429, "Too Many Requests", {}, None),
     "skipped — HTTP 429"),
    (urllib.error.URLError("timed out"), "skipped — fetch failed"),
])
async def test_search_web_survives_a_failing_result(monkeypatch, exc, expected):
    """A single result that cannot be fetched costs that result's extract and nothing
    else — the search must still return the ones that worked."""
    _ddgs_returning(monkeypatch, [
        {"title": "First", "href": "https://good.example/a", "body": "snippet a"},
        {"title": "Broken", "href": "https://blocked.example/b", "body": "snippet b"},
        {"title": "Last", "href": "https://good.example/c", "body": "snippet c"},
    ])
    monkeypatch.setitem(sys.modules, "trafilatura",
                        types.SimpleNamespace(extract=lambda raw: _long("EXTRACTED BODY")))

    def fake_fetch(url, *a, **k):
        if "blocked" in url:
            raise exc
        return b"<html>x</html>"
    monkeypatch.setattr(augment, "safe_fetch", fake_fetch)

    out = (await call("search_web", {"query": "anything"}))[0].text
    assert "Search failed" not in out, "one dead result aborted the whole search"
    assert expected in out
    # Both survivors are present: the one fetched before the failure and the one after.
    assert out.count("EXTRACTED BODY") == 2
    assert "https://good.example/c" in out


@pytest.mark.asyncio
async def test_search_web_reads_a_youtube_result_as_a_transcript(monkeypatch):
    """A watch page has no article body: trafilatura returns the cookie banner and
    the footer. The transcript API is already in this module for ingest_media, so a
    YouTube result has to take that path instead."""
    stub_dns(monkeypatch)
    _ddgs_returning(monkeypatch, [
        {"title": "Talk", "href": "https://www.youtube.com/watch?v=abc123", "body": "s"},
    ])
    _fake_transcript_api(monkeypatch, ["the spoken", "content"], expect_video_id="abc123")
    # What an HTML scrape of a watch page actually yields — must not be the answer.
    monkeypatch.setitem(sys.modules, "trafilatura", types.SimpleNamespace(
        extract=lambda raw: "Info Presse Urheberrecht Kontakt Impressum"))
    monkeypatch.setattr(augment, "safe_fetch", lambda u, *a, **k: b"<html>watch</html>")

    out = (await call("search_web", {"query": "anything"}))[0].text
    assert "the spoken content" in out
    assert "Urheberrecht" not in out, "the watch page was scraped instead of transcribed"


@pytest.mark.asyncio
async def test_search_web_youtube_result_without_a_transcript_is_not_fatal(monkeypatch):
    """Disabled subtitles or an age gate make one result contentless — the others
    still have to come back."""
    stub_dns(monkeypatch)
    _ddgs_returning(monkeypatch, [
        {"title": "Talk", "href": "https://youtu.be/abc123", "body": "s"},
        {"title": "Article", "href": "https://good.example/a", "body": "s"},
    ])

    class FailingYT:
        def fetch(self, video_id, languages=None):
            raise RuntimeError("Subtitles are disabled for this video")
    monkeypatch.setitem(sys.modules, "youtube_transcript_api",
                        types.SimpleNamespace(YouTubeTranscriptApi=FailingYT))
    monkeypatch.setitem(sys.modules, "trafilatura",
                        types.SimpleNamespace(extract=lambda raw: _long("ARTICLE BODY")))
    monkeypatch.setattr(augment, "safe_fetch", lambda u, *a, **k: b"<html>x</html>")

    out = (await call("search_web", {"query": "anything"}))[0].text
    assert "Search failed" not in out
    assert "no transcript available" in out
    assert "ARTICLE BODY" in out, "the contentless video result cost the article too"


@pytest.mark.asyncio
async def test_search_web_youtube_result_still_obeys_the_ssrf_filter(monkeypatch):
    """The transcript path bypasses safe_fetch, so the policy has to be applied here
    too — otherwise a search result is a way around AUGMENT_ALLOWED_DOMAINS."""
    stub_dns(monkeypatch)
    monkeypatch.setattr(config, "AUGMENT_ALLOWED_DOMAINS", ["blog.example"])
    _ddgs_returning(monkeypatch, [
        {"title": "Talk", "href": "https://www.youtube.com/watch?v=abc123", "body": "s"},
    ])
    _fake_transcript_api(monkeypatch, ["should not be reached"])
    monkeypatch.setitem(sys.modules, "trafilatura",
                        types.SimpleNamespace(extract=lambda raw: "x"))

    out = (await call("search_web", {"query": "anything"}))[0].text
    assert "AUGMENT_ALLOWED_DOMAINS" in out
    assert "should not be reached" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("chrome", ["Sign in", "Info Presse Urheberrecht Kontakt Impressum", ""])
async def test_search_web_reports_boilerplate_as_no_extract(monkeypatch, chrome):
    """A login wall extracts to "Sign in" and a consent page to a row of link labels.
    Passed through as a Content extract, that invites the caller to treat a page it
    never read as read — both observed live on real result URLs."""
    _ddgs_returning(monkeypatch, [
        {"title": "Walled", "href": "https://colab.example/nb", "body": "s"},
    ])
    monkeypatch.setitem(sys.modules, "trafilatura",
                        types.SimpleNamespace(extract=lambda raw: chrome))
    monkeypatch.setattr(augment, "safe_fetch", lambda u, *a, **k: b"<html>x</html>")

    out = (await call("search_web", {"query": "anything"}))[0].text
    assert "none extracted" in out
    if chrome:
        assert f"Content extract: {chrome}" not in out
    # The result itself stays listed — the URL is still worth handing back.
    assert "https://colab.example/nb" in out


@pytest.mark.asyncio
async def test_search_web_max_results_is_passed_through_and_clamped(monkeypatch):
    """Breadth is the caller's decision, but each result costs a fetch of up to
    MAX_FETCH_BYTES, so an unbounded value must be clamped rather than trusted."""
    seen = []

    class FakeDDGS:
        def text(self, query, max_results=3):
            seen.append(max_results)
            return []
    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=FakeDDGS))
    monkeypatch.setitem(sys.modules, "trafilatura",
                        types.SimpleNamespace(extract=lambda raw: None))

    await call("search_web", {"query": "q"})                      # default
    await call("search_web", {"query": "q", "max_results": 7})    # honoured
    await call("search_web", {"query": "q", "max_results": 500})  # clamped
    await call("search_web", {"query": "q", "max_results": 0})    # floored
    assert seen == [augment._DEFAULT_RESULTS, 7, augment._MAX_RESULTS, 1]


@pytest.mark.asyncio
async def test_search_web_no_results(monkeypatch):
    class FakeDDGS:
        def text(self, query, max_results=3):
            return []
    monkeypatch.setitem(sys.modules, "ddgs",
                        types.SimpleNamespace(DDGS=FakeDDGS))
    monkeypatch.setitem(sys.modules, "trafilatura",
                        types.SimpleNamespace(extract=lambda raw: None))
    res = await call("search_web", {"query": "nothing matches"})
    assert "No results found." in res[0].text


# ----------------- ingest_media -----------------
@pytest.mark.asyncio
async def test_ingest_media_article_uses_safe_fetch(monkeypatch):
    """A non-YouTube URL is fetched through safe_fetch (SSRF-guarded) and extracted."""
    monkeypatch.setitem(sys.modules, "trafilatura",
                        types.SimpleNamespace(extract=lambda raw: "ARTICLE TEXT"))
    seen = {}

    def fake_fetch(url, *a, **k):
        seen["url"] = url
        return b"<html>article</html>"
    monkeypatch.setattr(augment, "safe_fetch", fake_fetch)

    res = await call("ingest_media", {"url": "https://blog.example/post"})
    assert "ARTICLE TEXT" in res[0].text
    assert seen["url"] == "https://blog.example/post"  # went through safe_fetch


@pytest.mark.asyncio
async def test_ingest_media_blocked_url_returns_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "trafilatura",
                        types.SimpleNamespace(extract=lambda raw: "x"))

    def fake_fetch(url, *a, **k):
        raise ValueError("Security Error: private/internal target blocked")
    monkeypatch.setattr(augment, "safe_fetch", fake_fetch)

    res = await call("ingest_media", {"url": "http://127.0.0.1/secret"})
    assert "Security Error" in res[0].text


def _fake_transcript_api(monkeypatch, snippets, expect_video_id=None):
    class Snippet:
        def __init__(self, text): self.text = text

    class FakeYT:
        def fetch(self, video_id, languages=None):
            if expect_video_id is not None:
                assert video_id == expect_video_id
            return [Snippet(s) for s in snippets]
    monkeypatch.setitem(sys.modules, "youtube_transcript_api",
                        types.SimpleNamespace(YouTubeTranscriptApi=FakeYT))


@pytest.mark.asyncio
async def test_ingest_media_youtube_transcript(monkeypatch):
    """A YouTube URL is routed to the transcript API (1.x instance .fetch()),
    not safe_fetch. Snippets expose .text, not dict keys."""
    stub_dns(monkeypatch)
    _fake_transcript_api(monkeypatch, ["hello", "world"], expect_video_id="abc123")
    res = await call("ingest_media", {"url": "https://www.youtube.com/watch?v=abc123"})
    assert "hello world" in res[0].text


@pytest.mark.asyncio
async def test_ingest_media_youtube_shortlink_takes_the_id_from_the_path(monkeypatch):
    """youtu.be carries the id in the path, youtube.com in the ?v= query — the
    branch is picked by host, so both shapes have to keep working."""
    stub_dns(monkeypatch)
    _fake_transcript_api(monkeypatch, ["short"], expect_video_id="abc123")
    res = await call("ingest_media", {"url": "https://youtu.be/abc123"})
    assert "short" in res[0].text


def _stub_article(monkeypatch, body):
    monkeypatch.setitem(sys.modules, "trafilatura",
                        types.SimpleNamespace(extract=lambda raw: body))
    monkeypatch.setattr(augment, "safe_fetch", lambda u, *a, **k: b"<html>x</html>")


@pytest.mark.asyncio
async def test_ingest_media_transcript_cut_names_its_full_length(monkeypatch):
    """A multi-hour video exceeds any context window, so the transcript is cut — and
    must say by how much. Silently, the caller reads a fragment as the whole talk."""
    stub_dns(monkeypatch)
    monkeypatch.setattr(config, "MAX_INGEST_CHARS", 100)
    _fake_transcript_api(monkeypatch, ["x" * 4000])
    res = await call("ingest_media", {"url": "https://www.youtube.com/watch?v=abc123"})
    assert "YouTube Transcript (first 100 of 4000 chars)" in res[0].text
    assert len(res[0].text.split(":\n", 1)[1]) == 100


@pytest.mark.asyncio
async def test_ingest_media_article_cut_names_its_full_length(monkeypatch):
    monkeypatch.setattr(config, "MAX_INGEST_CHARS", 50)
    _stub_article(monkeypatch, "y" * 900)
    res = await call("ingest_media", {"url": "https://blog.example/post"})
    assert "Article Content (first 50 of 900 chars)" in res[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("url,label", [
    ("https://www.youtube.com/watch?v=abc123", "YouTube Transcript"),
    ("https://blog.example/post", "Article Content"),
])
async def test_ingest_media_does_not_annotate_what_it_did_not_cut(monkeypatch, url, label):
    """The common case must not grow a count that only ever restates the obvious."""
    stub_dns(monkeypatch)
    _fake_transcript_api(monkeypatch, ["short body"])
    _stub_article(monkeypatch, "short body")
    res = await call("ingest_media", {"url": url})
    assert res[0].text == f"{label}:\nshort body"


@pytest.mark.asyncio
async def test_ingest_media_cap_is_a_character_limit_not_the_download_limit(monkeypatch):
    """The two are different quantities. MAX_FETCH_BYTES bounds the DOWNLOAD, and the
    article branch never reaches it — extraction only ever shrinks its input — so
    using it on text meant no effective cap at all, and 5 MB of prose is far past any
    context window. MAX_INGEST_CHARS is what bounds the text."""
    monkeypatch.setattr(config, "MAX_INGEST_CHARS", 20)
    monkeypatch.setattr(config, "MAX_FETCH_BYTES", 10_000_000)
    _stub_article(monkeypatch, "z" * 5000)
    res = await call("ingest_media", {"url": "https://blog.example/post"})
    assert "(first 20 of 5000 chars)" in res[0].text


@pytest.mark.asyncio
async def test_ingest_media_youtube_branch_applies_the_ssrf_filter(monkeypatch):
    """The transcript branch fetches outside safe_fetch, so it must invoke the SSRF
    policy itself — a YouTube host resolving into a private range is refused."""
    stub_dns(monkeypatch, ip="169.254.169.254")   # link-local / metadata
    _fake_transcript_api(monkeypatch, ["should not be reached"])
    res = await call("ingest_media", {"url": "https://www.youtube.com/watch?v=abc123"})
    assert "Security Error" in res[0].text
    assert "should not be reached" not in res[0].text


@pytest.mark.asyncio
async def test_ingest_media_youtube_branch_honours_the_allowlist(monkeypatch):
    """AUGMENT_ALLOWED_DOMAINS has to bind here too — it was silently inapplicable
    while this branch skipped the filter."""
    stub_dns(monkeypatch)
    monkeypatch.setattr(config, "AUGMENT_ALLOWED_DOMAINS", ["blog.example"])
    _fake_transcript_api(monkeypatch, ["should not be reached"])
    res = await call("ingest_media", {"url": "https://www.youtube.com/watch?v=abc123"})
    assert "AUGMENT_ALLOWED_DOMAINS" in res[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://evil.example.com/?x=youtube.com",   # substring match in the query
    "https://youtube.com.evil.tld/watch?v=a",    # suffix-looking host
    "https://evilyoutu.be/abc123",               # no dot before the suffix
])
async def test_ingest_media_routes_lookalike_hosts_to_the_article_branch(monkeypatch, url):
    """The branch is chosen by parsed HOST, not by a substring of the URL — these
    are not YouTube and must take the safe_fetch path."""
    stub_dns(monkeypatch)
    monkeypatch.setitem(sys.modules, "trafilatura",
                        types.SimpleNamespace(extract=lambda raw: "ARTICLE TEXT"))
    seen = {}

    def fake_fetch(u, *a, **k):
        seen["url"] = u
        return b"<html>x</html>"
    monkeypatch.setattr(augment, "safe_fetch", fake_fetch)
    # A transcript API that would fail loudly if this took the YouTube branch.
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", types.SimpleNamespace(
        YouTubeTranscriptApi=lambda: (_ for _ in ()).throw(
            AssertionError("lookalike host reached the YouTube branch"))))

    res = await call("ingest_media", {"url": url})
    assert "ARTICLE TEXT" in res[0].text
    assert seen["url"] == url


# ----------------- analyze_github_repo -----------------
def _blob(path):
    return {"path": path, "type": "blob"}


def _fake_github(monkeypatch, tree=None, readme=b"# Readme body", truncated=False,
                 tree_exc=None, readme_exc=None):
    """Stand in for the two GitHub endpoints the handler fetches, by URL."""
    import json
    payload = json.dumps(
        {"tree": tree if tree is not None else [_blob("README.md"), _blob("src/app.py")],
         "truncated": truncated}).encode("utf-8")

    def fake_fetch(url, *a, **k):
        if "api.github.com" in url:
            if tree_exc:
                raise tree_exc
            return payload
        if readme_exc:
            raise readme_exc
        return readme
    monkeypatch.setattr(augment, "safe_fetch", fake_fetch)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://github.com/psf/requests",
    "https://github.com/psf/requests.git",
    "https://github.com/psf/requests/",
    "https://www.github.com/psf/requests/tree/main/src",
])
async def test_analyze_github_repo_accepts_the_usual_url_shapes(monkeypatch, url):
    """A repo URL arrives in several shapes — with .git, with a trailing slash, or
    deep-linked into a branch. All name the same repo and must all work."""
    _fake_github(monkeypatch)
    res = await call("analyze_github_repo", {"repo_url": url})
    assert "src/app.py" in res[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("url,expected", [
    ("https://gitlab.com/o/r", "Only github.com"),
    ("https://github.com/onlyowner", "names no repository"),
    ("http://192.168.0.10/repo.git", "Only github.com"),
])
async def test_analyze_github_repo_refuses_what_it_cannot_read(monkeypatch, url, expected):
    """A URL this tool cannot serve is refused by name, not attempted: the tree API
    would answer a confusing 404 for a non-GitHub host."""
    def boom(*a, **k):
        raise AssertionError("must not fetch")
    monkeypatch.setattr(augment, "safe_fetch", boom)
    res = await call("analyze_github_repo", {"repo_url": url})
    assert expected in res[0].text


@pytest.mark.asyncio
async def test_analyze_github_repo_spawns_no_subprocess(monkeypatch):
    """Reading the tree over the API is what removes this tool's whole class of
    clone-borne risk (arbitrary host-file reads via a planted symlink, filling the
    volume): nothing is executed and nothing is written to disk."""
    import subprocess
    _fake_github(monkeypatch)

    def boom(*a, **k):
        raise AssertionError("analyze_github_repo must not spawn a subprocess")
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)

    res = await call("analyze_github_repo", {"repo_url": "https://github.com/o/r"})
    assert "src/app.py" in res[0].text


@pytest.mark.asyncio
async def test_analyze_github_repo_names_the_full_size_when_it_cuts_the_tree(monkeypatch):
    """A cut file list must say how much it left out. Truncated silently, 300 paths
    read as the whole repository and the caller reasons about an architecture it
    never saw."""
    monkeypatch.setattr(augment, "_MAX_TREE_FILES", 3)
    _fake_github(monkeypatch, tree=[_blob(f"f{i}.py") for i in range(50)])
    res = await call("analyze_github_repo", {"repo_url": "https://github.com/o/r"})
    assert "Repository Structure (3 of 50 files)" in res[0].text


@pytest.mark.asyncio
async def test_analyze_github_repo_says_nothing_about_size_when_nothing_was_cut(monkeypatch):
    """The common case must not grow a count that only ever repeats itself."""
    _fake_github(monkeypatch, tree=[_blob("a.py"), _blob("b.py")])
    res = await call("analyze_github_repo", {"repo_url": "https://github.com/o/r"})
    assert "Repository Structure:" in res[0].text
    assert "of 2 files" not in res[0].text


@pytest.mark.asyncio
async def test_analyze_github_repo_passes_on_githubs_own_truncation(monkeypatch):
    """GitHub caps very large trees itself and says so. Dropping that flag would
    present a partial repository as a complete one."""
    _fake_github(monkeypatch, truncated=True)
    assert "truncated this tree" in (await call(
        "analyze_github_repo", {"repo_url": "https://github.com/o/r"}))[0].text


@pytest.mark.asyncio
async def test_analyze_github_repo_reports_a_cut_readme_with_its_length(monkeypatch):
    monkeypatch.setattr(augment, "_MAX_README_CHARS", 10)
    _fake_github(monkeypatch, readme=b"y" * 500)
    res = await call("analyze_github_repo", {"repo_url": "https://github.com/o/r"})
    assert "README (first 10 of 500 chars)" in res[0].text


@pytest.mark.asyncio
async def test_analyze_github_repo_takes_the_readme_from_the_repo_root(monkeypatch):
    """Only a root-level readme is the project's readme. A nested one is some other
    document, and passing it off as the readme misrepresents the repo."""
    _fake_github(monkeypatch, tree=[_blob("docs/README-contributing.md"), _blob("a.py")])
    res = await call("analyze_github_repo", {"repo_url": "https://github.com/o/r"})
    assert "none in the repository root" in res[0].text


@pytest.mark.asyncio
async def test_analyze_github_repo_keeps_the_tree_when_the_readme_fetch_fails(monkeypatch):
    """The tree is the substantial half of the answer, so a failed second fetch must
    cost only the README — and say that it did."""
    _fake_github(monkeypatch, readme_exc=urllib.error.HTTPError(
        "u", 500, "err", {}, None))
    res = await call("analyze_github_repo", {"repo_url": "https://github.com/o/r"})
    assert "src/app.py" in res[0].text
    assert "could not be fetched" in res[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("code,expected", [
    (404, "not found"),
    (403, "rate limit"),
    (429, "rate limit"),
])
async def test_analyze_github_repo_explains_the_routine_api_failures(monkeypatch, code, expected):
    """404 and 403 are the two everyday outcomes and they call for opposite
    responses — fix the URL versus wait — so each is named."""
    _fake_github(monkeypatch, tree_exc=urllib.error.HTTPError("u", code, "e", {}, None))
    res = await call("analyze_github_repo", {"repo_url": "https://github.com/o/r"})
    assert expected in res[0].text.lower()


@pytest.mark.asyncio
async def test_analyze_github_repo_still_obeys_the_domain_allowlist(monkeypatch):
    """The fetch runs through safe_fetch, so AUGMENT_ALLOWED_DOMAINS binds here as
    it does for every other external fetch."""
    stub_dns(monkeypatch)
    monkeypatch.setattr(config, "AUGMENT_ALLOWED_DOMAINS", ["blog.example"])
    res = await call("analyze_github_repo", {"repo_url": "https://github.com/o/r"})
    assert "AUGMENT_ALLOWED_DOMAINS" in res[0].text
