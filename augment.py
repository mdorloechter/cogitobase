"""Augmentation tools (ADR 7): read-only external inputs.

These fetch external data but never write to the vault/Mem0 autonomously — the
AI decides afterwards via write_note. Every fetch passes validate_external_url.
Heavy/optional imports are done lazily inside handlers so a missing
optional dependency only fails the specific tool, not server startup.
"""
import asyncio
import urllib.error
import urllib.parse

import config
from config import log
from registry import register, text, OUTCOME_REJECTED, OUTCOME_UNAVAILABLE
from security import validate_external_url, safe_fetch


_YOUTUBE_HOSTS = frozenset({"youtube.com", "youtu.be"})


def _host_is(parsed, *domains: str) -> bool:
    """True when the URL's HOST is one of `domains` or a subdomain of one.

    Compared against the parsed host, never as a substring of the URL: a substring
    test routes ``https://evil.example.com/?x=youtube.com`` into whichever branch
    it guards, and the dot in the suffix keeps ``youtube.com.evil.tld`` and
    ``evilyoutu.be`` out.
    """
    host = (parsed.hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


async def _youtube_transcript(url: str, parsed) -> str:
    """Transcript text for a YouTube URL, via youtube-transcript-api.

    This path fetches through the transcript API, not safe_fetch, so it has to
    invoke the SSRF policy itself — otherwise it is the one external fetch in the
    server that skips it, and AUGMENT_ALLOWED_DOMAINS silently would not apply here.
    Shared by ingest_media and search_web: an HTML scrape of a watch page returns
    the cookie banner and the footer, never the spoken content.
    """
    validate_external_url(url)
    from youtube_transcript_api import YouTubeTranscriptApi
    if _host_is(parsed, "youtu.be"):
        video_id = parsed.path[1:]
    else:
        qs = urllib.parse.parse_qs(parsed.query)
        video_id = qs.get("v", [""])[0]
    # youtube-transcript-api: instance .fetch() returns snippet objects
    # exposing .text.
    transcript = await asyncio.to_thread(
        lambda: YouTubeTranscriptApi().fetch(video_id, languages=["de", "en"])
    )
    return " ".join(snippet.text for snippet in transcript)


def _capped(label: str, body: str) -> list:
    """`body` under `label`, naming the full length whenever it had to be cut.

    A silent cut is the worse failure: the caller cannot tell 5% of a transcript from
    all of it, so it cannot decide to fetch the source itself. Both ingest branches
    share this, so one knob bounds them and their wording cannot drift apart.
    """
    cut = body[:config.MAX_INGEST_CHARS]
    if len(body) > len(cut):
        label = f"{label} (first {len(cut)} of {len(body)} chars)"
    return text(f"{label}:\n{cut}")


@register(
    "ingest_media", "Fetch transcript from YouTube URL or text from an article URL.",
    {"type": "object", "properties": {"url": {"type": "string", "description":
        "A YouTube watch URL (returns the transcript) or any article URL (returns its text). "
        "Fetched read-only — nothing is written to the vault."}}, "required": ["url"]},
)
async def ingest_media(arguments: dict) -> list:
    url = arguments["url"]
    try:
        parsed = urllib.parse.urlparse(url)
        if _host_is(parsed, *_YOUTUBE_HOSTS):
            text_out = await _youtube_transcript(url, parsed)
            return _capped("YouTube Transcript", text_out)
        else:
            import trafilatura
            # Fetch via safe_fetch (re-validated redirects + size cap),
            # then hand the raw HTML to trafilatura for extraction only.
            raw = await asyncio.to_thread(safe_fetch, url)
            extracted = trafilatura.extract(raw.decode("utf-8", errors="ignore"))
            if not extracted:
                return text("Failed to extract content from URL.", OUTCOME_UNAVAILABLE)
            return _capped("Article Content", extracted)
    except ValueError as e:
        # A rejected URL: wrong scheme, off the allowlist, resolving to a private IP.
        # The caller can send a different one, so it is not an outage.
        return text(str(e), OUTCOME_REJECTED)
    except Exception as e:
        log.exception("ingest_media failed")
        return text(f"Error extracting media: {e}", OUTCOME_UNAVAILABLE)


def _fetch_failure(exc: OSError) -> str:
    """A one-line reason for a single result's failed fetch.

    A status code is worth naming: a 403 from a bot filter reads very differently
    from a 404, and the caller can decide whether to fetch that URL itself. Every
    other OSError (timeout, DNS, reset — all urllib.error.URLError) degrades to its
    message. Only ValueError means the SSRF policy refused the URL.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    return f"fetch failed: {exc}"


_DEFAULT_RESULTS = 3
_MAX_RESULTS = 10
# Below this, an "extract" is navigation chrome, not content: a login wall yields
# "Sign in", a consent page a handful of link labels. Passing that on invites the
# caller to treat a page it never saw as read.
_MIN_EXTRACT_CHARS = 200


@register(
    "search_web",
    "Web search: up to N results with title, URL and a short extract. "
    "Fetch a result URL for depth.",
    {"type": "object", "properties": {
        "query": {"type": "string", "description": "The search query, as you would type it into "
                                                   "a search engine."},
        "max_results": {"type": "integer", "minimum": 1, "maximum": _MAX_RESULTS,
                        "description": f"Results to return (default {_DEFAULT_RESULTS})."},
    }, "required": ["query"]},
)
async def search_web(arguments: dict) -> list:
    from ddgs import DDGS
    import trafilatura
    try:
        # Clamp rather than trust: the schema bound is advertised, not enforced by the
        # transport, and each result costs a fetch of up to MAX_FETCH_BYTES.
        # `or` would fold an explicit 0 into the default and skip the floor below.
        raw_wanted = arguments.get("max_results")
        try:
            requested = _DEFAULT_RESULTS if raw_wanted is None else int(raw_wanted)
        except (TypeError, ValueError):
            requested = _DEFAULT_RESULTS
        wanted = max(1, min(requested, _MAX_RESULTS))
        results = await asyncio.to_thread(lambda: DDGS().text(arguments["query"], max_results=wanted))
        if not results:
            return text("No results found.")
        report = f"Search Results for '{arguments['query']}':\n\n"
        for res in results:
            href = res.get("href", "")
            report += f"Title: {res.get('title', '')}\nURL: {href}\nSnippet: {res.get('body', '')}\n\n"
            parsed = urllib.parse.urlparse(href)
            if _host_is(parsed, *_YOUTUBE_HOSTS):
                # A watch page has no article body to extract — trafilatura returns
                # the cookie banner and the footer links. The transcript API is the
                # only path to what the video actually says.
                try:
                    spoken = await _youtube_transcript(href, parsed)
                except ValueError as e:
                    report += f"Content extract: [skipped — {e}]\n\n"
                    continue
                except Exception as e:
                    # No transcript, disabled subtitles, age gate: this result has no
                    # usable content, which is not a reason to lose the others.
                    report += f"Content extract: [no transcript available: {e}]\n\n"
                    continue
                report += f"Transcript extract: {spoken[:1000]}...\n\n"
                continue
            try:
                # Safe_fetch re-validates each hop and caps size.
                raw = await asyncio.to_thread(safe_fetch, href)
            except ValueError:
                report += "Content extract: [skipped — URL blocked by SSRF policy]\n\n"
                continue
            except OSError as e:
                # One dead result must not lose the whole search. urllib raises
                # HTTPError (a 403 from a bot filter is routine on search results)
                # and URLError for transport failures; both are OSError, NOT
                # ValueError, so an SSRF-only except lets them abort every result
                # — including the ones already fetched.
                report += f"Content extract: [skipped — {_fetch_failure(e)}]\n\n"
                continue
            extracted = trafilatura.extract(raw.decode("utf-8", errors="ignore")) or ""
            if len(extracted.strip()) < _MIN_EXTRACT_CHARS:
                # Say so rather than emitting nothing: a missing line reads as "no
                # extract was attempted", and a two-word one as the page's content.
                report += "Content extract: [none extracted — no readable body]\n\n"
                continue
            report += f"Content extract: {extracted[:1000]}...\n\n"
        return text(report)
    except Exception as e:
        log.exception("search_web failed")
        return text(f"Search failed: {e}", OUTCOME_UNAVAILABLE)


_GITHUB_HOSTS = frozenset({"github.com"})
# How much of the tree and the README reach the caller. Both are display limits on
# data already in hand, so a cut is reported with the full size rather than hidden.
_MAX_TREE_FILES = 300
_MAX_README_CHARS = 3000


def _github_owner_repo(repo_url: str) -> tuple[str, str]:
    """Split a GitHub repo URL into (owner, repo). Raises ValueError otherwise.

    The host is checked against the parsed hostname, not as a substring, so this
    shares _host_is' guarantees. A non-GitHub host is refused by name: this tool
    reads GitHub's tree API, and silently trying it against another forge would
    fail with a confusing 404 instead of the actual reason.
    """
    parsed = urllib.parse.urlparse(repo_url)
    if not _host_is(parsed, *_GITHUB_HOSTS):
        raise ValueError(
            f"Only github.com repositories are supported (got '{parsed.hostname or repo_url}').")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("URL names no repository — expected https://github.com/<owner>/<repo>.")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo


def _github_readme_path(entries: list[dict]) -> str:
    """The path of the repo's ROOT readme, or "" if it has none.

    Restricted to top-level blobs: a repo without a root README should report none
    rather than pass off `docs/README-contributing.md` as the project's readme.
    Sorted so the choice does not depend on the order the API happens to return.
    """
    tops = sorted(e["path"] for e in entries
                  if e.get("type") == "blob"
                  and "/" not in e.get("path", "")
                  and e["path"].lower().startswith("readme"))
    return tops[0] if tops else ""


def _github_failure(exc: urllib.error.HTTPError) -> tuple[str, str]:
    """A caller-actionable reason for a failed GitHub API call, and its outcome label.

    A bare status code is not enough here: 404 and 403 are the two routine outcomes
    and they call for opposite responses — fix the URL versus wait. The label follows
    that same split, so a rate limit shows up in /metrics as the server being unable to
    serve rather than as a caller error it is powerless to correct.
    """
    if exc.code == 404:
        return "Repository not found, or not public.", OUTCOME_REJECTED
    if exc.code in (403, 429):
        return ("GitHub API rate limit reached (60 requests/hour for unauthenticated "
                "access). Try again later."), OUTCOME_UNAVAILABLE
    return f"GitHub API error: HTTP {exc.code}", OUTCOME_UNAVAILABLE


@register(
    "analyze_github_repo", "Read a public GitHub repo's file tree and README (no clone).",
    {"type": "object", "properties": {"repo_url": {"type": "string", "description":
        "A github.com repository URL, e.g. 'https://github.com/owner/repo'. Other hosts are "
        "refused; the repo must be public."}}, "required": ["repo_url"]},
)
async def analyze_github_repo(arguments: dict) -> list:
    import json
    try:
        owner, repo = _github_owner_repo(arguments["repo_url"])
    except ValueError as e:
        return text(str(e), OUTCOME_REJECTED)
    # The tree API returns the WHOLE tree in one response, so the structure needs no
    # clone: no subprocess, nothing written to disk, and the fetch goes through
    # safe_fetch like every other one — which pins the validated IP, so this tool no
    # longer carries a DNS-rebinding window of its own. `HEAD` as the tree-ish
    # resolves the default branch server-side, avoiding both an extra request for
    # `default_branch` and a hardcoded main/master.
    api = f"https://api.github.com/repos/{owner}/{repo}/git/trees/HEAD?recursive=1"
    try:
        raw = await asyncio.to_thread(safe_fetch, api)
        tree = json.loads(raw.decode("utf-8", errors="ignore"))
    except ValueError as e:
        return text(str(e), OUTCOME_REJECTED)
    except urllib.error.HTTPError as e:
        return text(*_github_failure(e))
    except Exception as e:
        log.exception("analyze_github_repo tree fetch failed")
        return text(f"Repo scan failed: {e}", OUTCOME_UNAVAILABLE)

    entries = tree.get("tree") or []
    files = sorted(e["path"] for e in entries if e.get("type") == "blob" and e.get("path"))

    readme = ""
    readme_path = _github_readme_path(entries)
    if readme_path:
        # A second fetch, because the tree carries paths and sizes but no content.
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{readme_path}"
        try:
            readme = (await asyncio.to_thread(safe_fetch, raw_url)).decode("utf-8", errors="ignore")
        except Exception:
            # The tree is the substantial half of the answer; losing the README must
            # not lose it. Reported below rather than passed off as an empty readme.
            log.exception("analyze_github_repo README fetch failed")
            readme = None

    shown = files[:_MAX_TREE_FILES]
    head = (f"Repository Structure ({len(shown)} of {len(files)} files)"
            if len(files) > len(shown) else "Repository Structure")
    report = f"{head}:\n" + "\n".join(shown)
    if tree.get("truncated"):
        # GitHub itself says the tree is incomplete; passing that on silently would
        # present a partial repo as the whole one.
        report += "\n[GitHub truncated this tree — the repository has more files.]"
    if readme_path and readme is None:
        report += f"\n\nREADME ({readme_path}): [could not be fetched]"
    elif readme_path:
        cut = readme[:_MAX_README_CHARS]
        label = (f"README (first {len(cut)} of {len(readme)} chars)"
                 if len(readme) > len(cut) else "README")
        report += f"\n\n{label}:\n{cut}"
    else:
        report += "\n\nREADME: [none in the repository root]"
    return text(report)
