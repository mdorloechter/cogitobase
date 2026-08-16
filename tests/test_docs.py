"""The shipped docs and examples must agree with the code they describe.

These tests read no vault; each compares a claim in a doc against the thing it
claims about. A README that promises behaviour the code does not have is a bug
with no stack trace, and only a test catches it.
"""
from pathlib import Path
import ast
import importlib.metadata
import importlib.util
import re
import subprocess
import pytest
import config
import registry
from conftest import PREAMBLE, call


# ----------------- .gitignore keeps every .env variant out ---------------------
# Asked of git itself: the ignore semantics (patterns, negation, precedence) are
# git's, so re-implementing them here would test our reading of the rules rather
# than the file. SECURITY.md points at .gitignore as the evidence that secrets
# cannot be committed, and .env.prod / .env.vps hold exactly the same secrets as
# .env.
@pytest.mark.parametrize("name", [".env", ".env.local", ".env.prod", ".env.vps"])
def test_gitignore_covers_every_env_variant(name):
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run(["git", "check-ignore", "-q", name], cwd=root)
    assert r.returncode == 0, f"{name} is not git-ignored — a secret file could be committed"


# ----------------- The shipped examples must match the documented Quickstart ---
# Both files are what an operator copies verbatim, so a setting that cannot work is
# followed rather than questioned: an unpublished port leaves the Quickstart's curl
# with nothing to reach, and a live GIT_REPO_URL placeholder points at someone else's
# repo.
def _compose_example():
    import yaml   # a pinned transitive dep of python-frontmatter
    root = Path(__file__).resolve().parent.parent
    return yaml.safe_load((root / "docker-compose.yml.example").read_text(encoding="utf-8"))


def test_compose_example_publishes_the_healthz_port():
    """README's Quickstart runs `curl localhost:8000/healthz`. With only `expose`,
    the port is reachable from inside the compose network and nowhere else."""
    published = _compose_example()["services"]["mcp-server"].get("ports") or []
    assert any(str(p).endswith(":8000") for p in published), \
        "no host port maps to 8000 — the documented healthz curl cannot connect"


def test_compose_example_binds_the_port_to_loopback():
    """The reverse proxy terminating TLS runs on the same host and is the first line
    of defence (README §5). A 0.0.0.0 bind would put the MCP endpoint on the network
    with the proxy bypassed."""
    for p in _compose_example()["services"]["mcp-server"].get("ports") or []:
        if str(p).endswith(":8000"):
            assert str(p).startswith("127.0.0.1:"), \
                f"port mapping '{p}' is not bound to loopback"


def test_compose_example_declares_no_obsolete_version():
    """`version:` is obsolete in the Compose Spec. Compose V2 ignores it and warns on
    every command, which trains the operator to read past its own warnings — including
    the ones about their config. It also reads as a compatibility floor the file does
    not have: nothing here works on a V1 that would honour '3.8'."""
    import yaml
    root = Path(__file__).resolve().parent.parent
    raw = yaml.safe_load((root / "docker-compose.yml.example").read_text(encoding="utf-8"))
    assert "version" not in raw, \
        f"docker-compose.yml.example declares version: {raw.get('version')!r}"


def test_compose_example_pins_every_image():
    """An unpinned image is a deployment that changes without a commit. It matters most
    for qdrant: it holds the index, so a major bump arriving on the next `docker compose
    pull` can change the storage format under a running vault, and SECURITY.md promises
    an exact pin for every direct dependency — an image is one.

    The tag is checked for existence, not for a value, so a deliberate bump does not have
    to edit this test. `latest` and a bare image name (which resolves to it) both fail.
    """
    unpinned = []
    for name, svc in _compose_example()["services"].items():
        image = svc.get("image")
        if not image:
            continue          # built from the local Dockerfile, nothing to pin
        tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else None
        if tag in (None, "latest"):
            unpinned.append(f"{name}: {image}")
    assert not unpinned, f"these images are not pinned to a version: {unpinned}"


def test_env_example_ships_no_active_git_repo_url():
    """A placeholder remote is not a working default: the clone fails, the server
    logs it and carries on WITHOUT a git repo, so nothing is ever committed or
    pushed. Unset is the honest state — startup then says 'running LOCAL-ONLY'."""
    root = Path(__file__).resolve().parent.parent
    for line in (root / ".env.example").read_text(encoding="utf-8").splitlines():
        assert not line.strip().startswith("GIT_REPO_URL="), \
            f"'{line.strip()}' is active — an unedited copy would fail to clone silently"


# Variables the code reads through a computed name, so no literal grep can find a
# matching line in .env.example. VAULT_RULE_* is documented by the profile block and
# one commented example; MEM0_TELEMETRY is set by us before importing Mem0, and the
# file documents opting back in.
_ENV_NAMES_WITHOUT_A_LITERAL_LINE = frozenset({"MEM0_TELEMETRY"})


# GIT_SSH_COMMAND is deliberately not offered as a knob: git_sync builds it from the
# mounted deploy key, and .env.example says so in prose instead.
_ENV_NAMES_NOT_A_KNOB = frozenset({"GIT_SSH_COMMAND"})


def _env_names_read_by_the_code() -> set[str]:
    root = Path(__file__).resolve().parent.parent
    names = set()
    for py in root.glob("*.py"):
        src = py.read_text(encoding="utf-8")
        names |= set(re.findall(r"""os\.(?:environ\.get|getenv)\(\s*["']([A-Z0-9_]+)["']""", src))
        names |= set(re.findall(r"""os\.environ\[\s*["']([A-Z0-9_]+)["']""", src))
    return names


def _env_names_named_in_the_example() -> set[str]:
    root = Path(__file__).resolve().parent.parent
    text = (root / ".env.example").read_text(encoding="utf-8")
    # Commented-out knobs count: they name the variable and its default, which is
    # what a reader needs. A bare mention in prose does not.
    return set(re.findall(r"^\s*#?\s*([A-Z0-9_]+)\s*=", text, re.MULTILINE))


def test_env_example_documents_every_variable_the_code_reads():
    """INSTALL.md tells the operator that .env.example is the list of what the server
    actually reads. An undocumented knob is one the operator cannot know to set —
    MCP_ALLOWED_HOSTS/ORIGINS decide whether DNS-rebinding protection is on at all,
    and QDRANT_HOST decides whether the store is reachable."""
    undocumented = (_env_names_read_by_the_code()
                    - _env_names_named_in_the_example()
                    - _ENV_NAMES_WITHOUT_A_LITERAL_LINE
                    - _ENV_NAMES_NOT_A_KNOB)
    assert not undocumented, \
        f"read by the code but absent from .env.example: {sorted(undocumented)}"


def test_env_example_names_no_variable_the_code_ignores():
    """The other direction: a knob that looks configurable but is read nowhere sends
    the operator chasing an effect that cannot happen."""
    read = _env_names_read_by_the_code()
    # VAULT_RULE_<NAME> reaches config.rule_strength through an f-string, so the
    # concrete names only exist in the profile table.
    stray = {v for v in _env_names_named_in_the_example() - read
             if not v.startswith("VAULT_RULE_")} - _ENV_NAMES_WITHOUT_A_LITERAL_LINE
    assert not stray, f"named in .env.example but read nowhere: {sorted(stray)}"


# Which ADR each module's docstring claims to implement, and a phrase from that ADR's
# heading. Four of these citations pointed at an unrelated ADR — a number is easy to
# get wrong and nothing reads it back, so the intent is pinned here. A number alone
# would not be enough: the wrong citations all named ADRs that exist.
_MODULE_ADR_CLAIMS = [
    ("git_sync.py", 4, "Git as Single Source of Truth"),
    ("vault.py", 4, "Git as Single Source of Truth"),
    ("skills.py", 3, "Skills: Catalog Push, Body Pull"),
    ("memory.py", 6, "Soft/Hard Enforcement"),
    ("augment.py", 7, "Read-Only + SSRF-Safe Fetch"),
    ("validation.py", 5, "AI-First Vault Rules"),
]


@pytest.mark.parametrize("module,adr,heading_phrase", _MODULE_ADR_CLAIMS)
def test_module_docstring_cites_the_right_adr(module, adr, heading_phrase):
    root = Path(__file__).resolve().parent.parent
    headings = dict(re.findall(r"^## ADR (\d+) — (.+)$",
                               (root / "ARCHITECTURE.md").read_text(encoding="utf-8"),
                               re.MULTILINE))
    assert heading_phrase in headings.get(str(adr), ""), \
        f"ADR {adr} is '{headings.get(str(adr))}', not the one {module} means"
    docstring = ast.get_docstring(ast.parse((root / module).read_text(encoding="utf-8"))) or ""
    assert re.search(rf"ADR #?{adr}\b", docstring), \
        f"{module}'s docstring no longer cites ADR {adr}"


def test_every_cited_adr_exists():
    """Weaker than the table above but covers every citation, including the inline ones
    in function bodies: a number that names no ADR sends the reader nowhere."""
    root = Path(__file__).resolve().parent.parent
    known = set(re.findall(r"^## ADR (\d+) —",
                           (root / "ARCHITECTURE.md").read_text(encoding="utf-8"), re.MULTILINE))
    for py in root.glob("*.py"):
        for cited in re.findall(r"ADR #?(\d+)", py.read_text(encoding="utf-8")):
            assert cited in known, f"{py.name} cites ADR {cited}, which does not exist"


def _adr_15():
    root = Path(__file__).resolve().parent.parent
    text = (root / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "## ADR 15 —" in text, "ARCHITECTURE.md no longer has an ADR 15"
    return text.split("## ADR 15 —")[1].split("\n## ADR ")[0]


def test_the_protocol_revision_adr_15_claims_is_the_one_the_sdk_speaks():
    """ADR 15 names the newest protocol revision cogitobase serves, and INSTALL.md §6
    turns that into a compatibility promise an operator acts on.

    The revision is not ours to choose: the pinned SDK decides it, and nothing in a doc
    is consulted when the pin moves. Asserted against the SDK's own constant rather than
    a literal, so a bump to a line that speaks a different revision fails here until the
    ADR and the install guide say what the server actually answers.
    """
    import mcp.types
    claimed = re.search(r"up to and including `(\d{4}-\d{2}-\d{2})`", _adr_15())
    assert claimed, "ADR 15 no longer names the revision it serves in the expected form"
    assert claimed.group(1) == mcp.types.LATEST_PROTOCOL_VERSION, (
        f"ADR 15 claims to serve {claimed.group(1)}, but the pinned SDK speaks "
        f"{mcp.types.LATEST_PROTOCOL_VERSION}")

    root = Path(__file__).resolve().parent.parent
    install = (root / "INSTALL.md").read_text(encoding="utf-8")
    assert f"`{claimed.group(1)}`" in install, \
        f"INSTALL.md does not tell an operator which revision ({claimed.group(1)}) is served"


def test_the_pin_adr_15_rests_on_is_the_pin_requirements_actually_carries():
    """ADR 15's whole argument is the exact pin: `pip install mcp` resolves to a line that
    speaks a different protocol era, so the version is the only thing holding the era in
    place.

    A range or a bump would falsify the ADR without touching it, and the failure would
    not surface at install time — it surfaces as a client that no longer connects. So the
    ADR must name the pin, and requirements.txt must carry exactly that.
    """
    root = Path(__file__).resolve().parent.parent
    pinned = re.search(r"^mcp==(\S+)$",
                       (root / "requirements.txt").read_text(encoding="utf-8"), re.MULTILINE)
    assert pinned, "requirements.txt no longer pins mcp to an exact version"
    assert f"`mcp=={pinned.group(1)}`" in _adr_15(), (
        f"ADR 15 does not name the pin it rests on — requirements.txt has "
        f"mcp=={pinned.group(1)}")
    assert importlib.metadata.version("mcp") == pinned.group(1), (
        f"the installed mcp is {importlib.metadata.version('mcp')}, not the pinned "
        f"{pinned.group(1)}")


@pytest.mark.asyncio
async def test_the_documented_obsidian_promise_holds_on_disk():
    """ADR 4 promises an edit leaves everything it does not change as text, which is the
    one claim a reader of a vault-editing tool has to be able to trust. Asserted against
    the real doc AND the real write path, so neither half can drift from the other: the
    values below are exactly the ones ADR 4 names as being read wrong by YAML."""
    root = Path(__file__).resolve().parent.parent
    adr4 = (root / "ARCHITECTURE.md").read_text(encoding="utf-8").split("## ADR 4")[1] \
        .split("## ADR 5")[0]
    assert "as text" in adr4 or "as **text**" in adr4, "ADR 4 no longer makes the promise"
    (config.BRAIN_DIR / "concepts").mkdir(parents=True, exist_ok=True)
    path = config.BRAIN_DIR / "concepts" / "promise.md"
    quirks = [ln for ln in ("start: 10:30", "id: 0123", "ver: 1.10", "done: no")
              if ln.split(":")[1].strip() in adr4]
    assert len(quirks) == 4, f"ADR 4 no longer names these values: {quirks}"
    body = f"# Promise\n\n{PREAMBLE}\nhand-written body\n"
    path.write_text("---\ntype: concept\n" + "\n".join(quirks) + f"\n---\n\n{body}",
                    encoding="utf-8")
    await call("append_to_note", {"filename": "promise", "content": "appended"})
    saved = path.read_text(encoding="utf-8")
    for line in quirks:
        assert line in saved, f"{line!r} was rewritten, against ADR 4:\n{saved}"
    assert body in saved, f"the body was not carried over verbatim:\n{saved}"


def test_the_product_name_is_written_in_lowercase_everywhere():
    """The wordmark is lowercase in every position (BRANDING.md §1). That only reads as
    deliberate when it is exceptionless — one capitalised instance makes the rest look
    like typos — and it is exactly the kind of thing a later edit reintroduces by
    reflex, in a file nobody rereads. Identifiers are exempt where their own language's
    convention is stronger: a Prometheus alert name stays CamelCase."""
    root = Path(__file__).resolve().parent.parent
    # Only what the repo actually ships. A local vault-data/ holds the user's own notes,
    # where the name is theirs to spell however they like.
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True,
                             text=True, check=True).stdout.split("\0")
    offenders = []
    for name in filter(None, tracked):
        path = root / name
        if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".ico", ".pdf"):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(content.splitlines(), 1):
            for hit in re.finditer(r"(?<![A-Za-z])[Cc]ogito[Bb]ase(?![A-Za-z])", line):
                if hit.group() != "cogitobase":
                    offenders.append(f"{name}:{n}: {hit.group()}")
    assert not offenders, "the wordmark must be lowercase:\n" + "\n".join(offenders)
    # The rule itself has to stay written down, or the next contributor cannot know it.
    branding = (root / "assets" / "BRANDING.md").read_text(encoding="utf-8")
    assert "always lowercase" in branding.lower(), \
        "BRANDING.md no longer states the lowercase rule this test enforces"


def test_every_place_that_states_a_version_states_the_same_one():
    """config.__version__ is the single source, and the MCP handshake reports it so a
    client can tell which build it talks to. The copies drift silently: a README badge or
    an image label naming a version that was never released is worse than none."""
    root = Path(__file__).resolve().parent.parent
    v = config.__version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), f"not a semver version: {v!r}"

    # The handshake: the SDK falls back to reporting its OWN version when unset.
    import server
    assert server.app.version == v, "the MCP handshake does not report config.__version__"

    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    label = re.search(r'org\.opencontainers\.image\.version="([^"]+)"', dockerfile)
    assert label and label.group(1) == v, f"Dockerfile labels {label and label.group(1)!r}"

    readme = (root / "README.md").read_text(encoding="utf-8")
    badge = re.search(r"badge/version-([\d.]+)-", readme)
    assert badge and badge.group(1) == v, f"README badge shows {badge and badge.group(1)!r}"
    # Every OTHER version-shaped string in the badge row would be a second claim.
    assert f"**{v}**" in readme, f"README does not state {v} in the status section"


def test_no_doc_uses_wording_branding_forbids():
    """BRANDING.md §8 bans intensifiers and absolutes another document contradicts, and
    §1 bans the no-egress claims PRIVACY.md would refute. These are exactly the phrases a
    later edit reaches for by reflex, so the ban is only real if something checks it. The
    forbidden strings are read out of BRANDING.md, so the rule and its enforcement cannot
    drift apart."""
    root = Path(__file__).resolve().parent.parent
    branding_path = root / "assets" / "BRANDING.md"
    # Collapsed to one line first: the guidelines wrap, so a quoted phrase can carry a
    # line break the reader never sees.
    branding = re.sub(r"\s+", " ", branding_path.read_text(encoding="utf-8"))
    # The guidelines quote every banned phrase — that is where the list lives.
    banned = {q.lower() for q in re.findall(r'"([^"]{4,40})"', branding)
              if q.lower() in ("incredibly powerful", "very easy", "indestructible",
                               "fully private", "nothing leaves your server",
                               "zero third parties")}
    assert len(banned) == 6, f"BRANDING.md no longer quotes all six phrases: {banned}"
    tracked = subprocess.run(["git", "ls-files", "-z", "*.md"], cwd=root,
                             capture_output=True, text=True, check=True).stdout
    offenders = []
    for name in filter(None, tracked.split("\0")):
        if (root / name) == branding_path:
            continue          # the guidelines have to name what they ban
        for n, line in enumerate((root / name).read_text(encoding="utf-8").splitlines(), 1):
            for phrase in banned:
                if phrase in line.lower():
                    offenders.append(f"{name}:{n}: {phrase}")
    assert not offenders, ("BRANDING.md forbids this wording:\n" + "\n".join(offenders))


@pytest.mark.asyncio
async def test_the_readme_promise_that_no_other_note_is_touched_holds():
    """The README tells a reader no pre-existing note is moved, renamed or rewritten
    behind their back. That is the load-bearing promise of a tool that edits someone's
    vault, and the only one they cannot verify before trusting it — so it is asserted
    against the real write paths, not just read out of the doc."""
    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "No pre-existing note is ever moved, renamed, or rewritten" in readme, \
        "README no longer makes the promise this test enforces"

    (config.BRAIN_DIR / "concepts").mkdir(parents=True, exist_ok=True)
    args = {"vault": "brain", "title": "T", "tags": [], "type_meta": "concept",
            "content": "## AI Summary\nSummary line.\n\nBody text."}
    for name in ("target", "bystander", "linker"):
        await call("write_note", {**args, "filename": name})
    # A hand-authored note the server never wrote, with a link into the rename target.
    (config.BRAIN_DIR / "concepts" / "linker.md").write_text(
        f"---\ntype: concept\n---\n\n# Linker\n\n{PREAMBLE}\nSee [[target]].\n",
        encoding="utf-8")

    def snapshot():
        return {p.relative_to(config.BRAIN_DIR).as_posix(): p.read_bytes()
                for p in sorted(config.BRAIN_DIR.rglob("*.md"))}

    before = snapshot()
    await call("write_note", {**args, "filename": "target",
                              "content": "## AI Summary\nS.\n\nOverwritten."})
    await call("append_to_note", {"filename": "target", "content": "appended"})
    await call("rename_note", {"old_filename": "target", "new_filename": "renamed"})
    await call("reindex_vault", {})
    after = snapshot()

    # Only the note each call named may differ; the rename is the one path that changes
    # a filename, and only its own.
    assert set(after) - set(before) == {"concepts/renamed.md"}
    assert set(before) - set(after) == {"concepts/target.md"}
    for name in ("concepts/bystander.md", "concepts/linker.md"):
        assert after[name] == before[name], f"{name} was rewritten"


def test_every_image_a_doc_points_at_is_actually_shipped():
    """A README hero that 404s is the first thing a visitor sees. Local image paths are
    checked against git, not the filesystem, because an asset that exists only on the
    author's disk renders as a broken image for everyone else."""
    root = Path(__file__).resolve().parent.parent
    tracked = set(filter(None, subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True,
        check=True).stdout.split("\0")))
    missing = []
    for doc in (p for p in tracked if p.endswith(".md") and "/" not in p):
        for n, line in enumerate((root / doc).read_text(encoding="utf-8").splitlines(), 1):
            # A reference inside a code span is an illustration of the syntax, not a
            # picture the reader is meant to see.
            line = re.sub(r"`[^`]*`", "", line)
            for ref in (re.findall(r'<img[^>]+src="([^"]+)"', line)
                        + re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", line)):
                if ref.startswith(("http://", "https://", "data:")):
                    continue
                if ref.lstrip("./") not in tracked:
                    missing.append(f"{doc}:{n}: {ref}")
    assert not missing, "these images are referenced but not committed:\n" + "\n".join(missing)


def test_the_brand_assets_still_match_their_generator():
    """`assets/` is generated by `assets/brand.py`, and BRANDING.md tells the next
    contributor to change the generator rather than an export. That instruction is only
    true while the two agree: an SVG touched by hand is silently overwritten by the next
    run, and one regenerated with an edited palette leaves every PNG stale. Regenerating
    into memory and comparing is the cheapest way to keep the claim honest."""
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("brand", root / "assets" / "brand.py")
    brand = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(brand)
        brand.ASSETS["logo"][0]()          # pulls in the font, which ships separately
    except ImportError as e:
        # fonttools and font-roboto are needed to generate, not to run the server, so
        # they are not suite dependencies. CI installs them; a bare checkout skips.
        pytest.skip(f"brand generator dependencies not installed: {e}")
    for name, (build, _) in brand.ASSETS.items():
        shipped = (root / "assets" / f"{name}.svg")
        assert shipped.exists(), f"{shipped.name} is missing — run python assets/brand.py"
        assert shipped.read_text(encoding="utf-8") == build(), (
            f"{shipped.name} differs from what assets/brand.py produces. Edit the "
            f"generator, then re-run it — the exports are derived files.")
    # Every colour a mark uses has to be a documented one, or the palette table in
    # BRANDING.md stops describing the actual brand.
    documented = {c.lower() for c in re.findall(r"`(#[0-9A-Fa-f]{6})`",
                  (root / "assets" / "BRANDING.md").read_text(encoding="utf-8"))}
    used = {c.lower() for name in brand.ASSETS
            for c in re.findall(r"#[0-9A-Fa-f]{6}",
                                (root / "assets" / f"{name}.svg").read_text(encoding="utf-8"))}
    assert used <= documented, (
        f"these colours are in the assets but not in the BRANDING.md palette: "
        f"{sorted(used - documented)}")


def _load_generator(root, name):
    """Import an assets/ generator, skipping if its generate-only deps are absent."""
    spec = importlib.util.spec_from_file_location(name, root / "assets" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # brand.py is imported by name from diagrams.py, which only resolves with assets/
    # on the path — the generators are run as scripts from that directory.
    import sys
    sys.path.insert(0, str(root / "assets"))
    try:
        spec.loader.exec_module(module)
    except ImportError as e:
        pytest.skip(f"{name} generator dependencies not installed: {e}")
    finally:
        sys.path.remove(str(root / "assets"))
    return module


def test_the_diagrams_still_match_their_generator():
    """The diagrams are derived from `assets/diagrams.py`, on the same terms as the marks:
    an SVG edited by hand is silently overwritten by the next run.

    They are checked separately from the marks because their failure mode is worse. A logo
    is decoration; a diagram carries claims — which components exist, what talks to what,
    where data leaves the host. A layout tweak that outruns its export leaves a picture in
    the README that argues for an architecture the code no longer has, and a reader trusts
    a diagram more readily than a paragraph.
    """
    root = Path(__file__).resolve().parent.parent
    diagrams = _load_generator(root, "diagrams")
    for name, (build, _) in diagrams.DIAGRAMS.items():
        shipped = root / "assets" / f"{name}.svg"
        assert shipped.exists(), f"{shipped.name} is missing — run python assets/diagrams.py"
        assert shipped.read_text(encoding="utf-8") == build(), (
            f"{shipped.name} differs from what assets/diagrams.py produces. Edit the "
            f"generator, then re-run it — the exports are derived files.")


def test_every_diagram_stays_inside_the_documented_palette_and_size():
    """BRANDING.md §3 calls its table exhaustive ("Diagrams are brand surfaces too") and §7
    caps every PNG at 300 KB.

    A diagram is where both rules break first: it needs more distinctions than a logo does,
    and reaching for a warm accent to mark one box is the obvious way to get one. The cap
    is the other half — a diagram is the widest asset shipped, so it is the one that grows
    past what a README can load.
    """
    root = Path(__file__).resolve().parent.parent
    branding = (root / "assets" / "BRANDING.md").read_text(encoding="utf-8")
    documented = {c.lower() for c in re.findall(r"`(#[0-9A-Fa-f]{6})`", branding)}
    assert len(documented) >= 7, f"BRANDING.md no longer lists a palette: {documented}"
    cap = re.search(r"under (\d+) KB", branding)
    assert cap, "BRANDING.md no longer states the PNG size cap this test enforces"
    limit = int(cap.group(1)) * 1024

    diagrams = _load_generator(root, "diagrams")
    for name in diagrams.DIAGRAMS:
        svg = (root / "assets" / f"{name}.svg").read_text(encoding="utf-8")
        used = {c.lower() for c in re.findall(r"#[0-9A-Fa-f]{6}", svg)}
        assert used <= documented, (
            f"{name}.svg uses colours the BRANDING.md palette does not list: "
            f"{sorted(used - documented)}")
        png = root / "assets" / f"{name}.png"
        assert png.exists(), f"{png.name} is missing — run python assets/diagrams.py"
        assert png.stat().st_size <= limit, (
            f"{png.name} is {png.stat().st_size / 1024:.0f} KB, over the "
            f"{limit / 1024:.0f} KB cap")


def test_the_diagrams_name_only_clients_install_documents():
    """The diagrams draw one box per AI client, and INSTALL.md §6 is where an operator goes
    to connect one. A client in the picture with no section behind it is a promise the docs
    never keep — and the connection steps are the only thing that makes two clients belong
    in the same paragraph, so a box that merges two products asserts a kinship they lack.

    Matched case-sensitively: each vendor spells its own product one way, and a diagram is
    read as the authoritative spelling. Two files disagreeing on it makes one of them wrong
    wherever the reader looks next.
    """
    root = Path(__file__).resolve().parent.parent
    diagrams = _load_generator(root, "diagrams")
    install = (root / "INSTALL.md").read_text(encoding="utf-8")
    # Only the headings that introduce connection steps count. Searching the whole file
    # would accept a name that appears solely in a config path (`.opencode/`), which is a
    # lowercase package name, not the product — and would pass a diagram spelled that way.
    headings = "\n".join(re.findall(r"^### Example for .*$", install, re.MULTILINE))
    assert headings, "INSTALL.md §6 no longer has per-client connection examples"
    assert diagrams.CLIENTS, "assets/diagrams.py no longer names the clients it draws"
    for name in diagrams.CLIENTS:
        assert name in headings, (
            f"the diagrams draw a box for {name!r}, but no INSTALL.md §6 example spells it "
            f"that way — either §6 gains connection steps for it, or the box goes")


@pytest.mark.parametrize("doc", ["README.md", "INSTALL.md"])
def test_docs_do_not_claim_the_port_is_unpublished(doc):
    """The compose template publishes 8000 on loopback so the documented healthz curl
    works. Three passages still said the port was 'not published to the host' / 'only
    in the internal Docker network', which sends an operator looking for a mapping
    that is already there — and understates what is bound while reading as if it
    overstated the isolation."""
    root = Path(__file__).resolve().parent.parent
    published = any(str(p).endswith(":8000")
                    for p in _compose_example()["services"]["mcp-server"].get("ports") or [])
    if not published:
        pytest.skip("compose publishes no host port — the 'internal only' wording is true")
    text = (root / doc).read_text(encoding="utf-8").lower()
    for claim in ("not** published to the host", "only exposes port `8000` on the internal",
                  "port 8000 only in the internal"):
        assert claim not in text, f"{doc} still claims the port is unpublished: '{claim}'"


def test_readme_tool_count_matches_the_registry():
    """README §6 states the number of registered tools. It is worth stating — it tells
    a reader the size of the surface — but a hand-written count is a claim nothing reads
    back: registering a tool does not touch the README, so the number goes stale without
    anything failing. Every tool module is imported at the top of this file, so the
    registry here is the full one."""
    root = Path(__file__).resolve().parent.parent
    claimed = re.search(r"registers \*\*(\d+) tools\*\*",
                        (root / "README.md").read_text(encoding="utf-8"))
    assert claimed, "README no longer states a tool count in the expected form"
    assert int(claimed.group(1)) == len(registry.all_tools()), \
        f"README claims {claimed.group(1)} tools, registry has {len(registry.all_tools())}"


def test_security_md_does_not_promise_a_full_lockfile():
    """requirements.txt pins direct dependencies only — the installed tree is an order
    of magnitude larger. SECURITY.md must not read as a lockfile guarantee, so it has to
    name the limit wherever it describes the pinning (verified against the real file)."""
    root = Path(__file__).resolve().parent.parent
    direct = [re.split(r"[=<>!\[; ]", line.strip(), maxsplit=1)[0]
              for line in (root / "requirements.txt").read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.strip().startswith("#")]
    known = {n.lower().replace("_", "-") for n in direct}
    unpinned = set()
    for name in direct:
        try:
            requires = importlib.metadata.requires(name) or []
        except importlib.metadata.PackageNotFoundError:
            continue   # a dependency this environment doesn't install
        for req in requires:
            if ";" in req and "extra" in req.split(";", 1)[1]:
                continue   # optional extra, not installed by a plain -r install
            dep = re.split(r"[=<>!\[; ()]", req, maxsplit=1)[0].strip().lower().replace("_", "-")
            if dep and dep not in known:
                unpinned.add(dep)
    if not unpinned:
        pytest.skip("every transitive dependency is pinned — the claim would be accurate")
    text = (root / "SECURITY.md").read_text(encoding="utf-8")
    section = text.split("## Dependencies", 1)[1]
    assert "transitive" in section.lower(), (
        f"SECURITY.md claims reproducible pinning without naming the "
        f"{len(unpinned)} unpinned transitive dependencies, e.g. {sorted(unpinned)[:5]}")


def test_gitignore_keeps_the_example_tracked():
    """The negation must survive: .env.example carries no secrets and is the file
    operators copy, so ignoring it would break the documented setup."""
    root = Path(__file__).resolve().parent.parent
    ignored = subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=root)
    assert ignored.returncode == 1, ".env.example must NOT be ignored"
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env.example"],
                             cwd=root, capture_output=True)
    assert tracked.returncode == 0, ".env.example must be tracked"


def _docs_about_this_repo():
    """The tracked Markdown that documents THIS repository, from the root and .github.

    `seed-skills/*.md` are deliberately not here: they are payload the server installs
    into the operator's vault, so their relative paths (`../media/diagram.png`) describe
    that vault's layout and resolve against it, not against this tree.
    """
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(["git", "ls-files", "-z", "*.md", ".github/**/*.md"], cwd=root,
                         capture_output=True, text=True, check=True)
    return root, [p for p in out.stdout.split("\0")
                  if p and (p.count("/") == 0 or p.startswith(".github/"))]


def test_every_repo_relative_doc_link_resolves():
    """A link to a file the repo does not hold is a dead end for the reader.

    Only targets that land inside the tree are checked: `../../security/advisories/new`
    is resolved by github.com rather than from disk, and flagging it would need an
    exemption list — which is the thing that stops being maintained.
    """
    root, docs = _docs_about_this_repo()
    assert len(docs) >= 8, f"the doc set has stopped matching: {docs}"
    dead = []
    for doc in docs:
        path = root / doc
        for match in re.finditer(r"\]\(([^)\s]+)\)", path.read_text(encoding="utf-8")):
            target = match.group(1).split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (path.parent / target).resolve()
            if root not in resolved.parents and resolved != root:
                continue   # points out of the tree, e.g. a GitHub-relative URL
            if not resolved.exists():
                dead.append(f"{doc} -> {target}")
    assert not dead, f"these links point at files the repo does not hold: {sorted(dead)}"


def test_no_tracked_file_points_at_a_changelog():
    """The project ships no changelog, so nothing may send a reader looking for one.

    Separate from the link test because a mention is not always a link: a sentence
    naming the file, or an assertion that a version heading appears in it, leaves the
    same reader looking for something that is not there. This file is excluded — it is
    where the rule is stated, so matching itself would make the test unsatisfiable.
    """
    root = Path(__file__).resolve().parent.parent
    this_file = Path(__file__).resolve().relative_to(root).as_posix()
    out = subprocess.run(["git", "grep", "-l", "-i", "changelog", "--", ".", f":!{this_file}"],
                         cwd=root, capture_output=True, text=True)
    assert out.returncode == 1, f"CHANGELOG is still referenced by: {out.stdout.split()}"
