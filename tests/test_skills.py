"""The skills layer: authoring, retrieval, seeding, and the context bootstrap.

Skills are git-persisted and shared across clients, so a seed must never silently
overwrite an edited skill, and the catalog must stay body-free.
"""
import pytest
import config
import skills
from conftest import call


# ----------------- prompt injects identity (+ skill catalog) -----------------
def test_build_prompt_injects_identity():
    (config.CONTEXT_DIR / "IDENTITY.md").write_text(
        "I am a backend engineer who writes type-safe Python.", encoding="utf-8")
    text = skills.build_prompt()
    assert "WHO I AM" in text
    assert "backend engineer" in text


def test_build_prompt_includes_skill_catalog():
    (config.SKILLS_DIR / "review-pr.md").write_text(
        "---\nname: review-pr\ndescription: d\nwhen_to_use: reviewing a PR\nversion: 1\n---\n"
        "UNIQUE_SKILL_BODY_MARKER step one",
        encoding="utf-8")
    text = skills.build_prompt()
    assert "AVAILABLE SKILLS" in text
    assert "review-pr" in text
    assert "reviewing a PR" in text
    # Catalog must NOT leak the skill body into the prompt (only name + when_to_use).
    assert "UNIQUE_SKILL_BODY_MARKER" not in text


def test_skill_catalog_skips_a_symlinked_skill(tmp_path):
    """A symlink in the skills dir must not be parsed: the catalog is pushed into
    every get_core_context call, so its content would be disclosed and treated as
    instructions on every session start."""
    outside = tmp_path / "outside.md"
    outside.write_text(
        "---\nname: evil\ndescription: d\nwhen_to_use: OUTSIDE_SKILL_MARKER\nversion: 1\n---\nbody",
        encoding="utf-8")
    try:
        (config.SKILLS_DIR / "evil.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation not permitted in this environment")
    assert "OUTSIDE_SKILL_MARKER" not in skills.build_prompt()
    assert "evil" not in {s["name"] for s in skills.list_skill_meta()}


# ----------------- get_core_context (one-call bootstrap) -----------------
@pytest.mark.asyncio
async def test_get_core_context_returns_full_live_context():
    """The tool returns identity + AI-first rules + skill catalog in one call,
    but never a skill BODY (bodies stay pull-only via get_skill)."""
    (config.CONTEXT_DIR / "IDENTITY.md").write_text(
        "I am a backend engineer who writes type-safe Python.", encoding="utf-8")
    await call("write_skill", {"name": "review-pr", "description": "d",
                               "when_to_use": "reviewing a PR", "body": "CORE_CTX_BODY_MARKER step one"})
    res = await call("get_core_context", {})
    txt = res[0].text
    assert "backend engineer" in txt          # live identity
    assert "AI-FIRST VAULT RULES" in txt       # meta rules
    assert "review-pr" in txt                   # catalog entry
    assert "CORE_CTX_BODY_MARKER" not in txt    # body is NOT pushed


def test_static_instructions_are_trigger_only_no_catalog_or_identity():
    """The `instructions` field carries the get_core_context bootstrap trigger ONLY —
    kept to that one load-bearing paragraph so client truncation has nothing to clip.
    Neither the skill catalog nor the live-editable identity is frozen in; both are
    pulled fresh via get_core_context. README §6 and ADR 14 describe it that way."""
    (config.CONTEXT_DIR / "IDENTITY.md").write_text(
        "SECRET_IDENTITY_MARKER backend engineer.", encoding="utf-8")
    (config.SKILLS_DIR / "review-pr.md").write_text(
        "---\nname: review-pr\ndescription: d\nwhen_to_use: reviewing a PR\nversion: 1\n---\nbody",
        encoding="utf-8")
    instr = skills.build_static_instructions()
    assert "get_core_context" in instr                 # bootstrap trigger present
    assert "review-pr" not in instr                    # catalog NOT frozen in
    assert "SECRET_IDENTITY_MARKER" not in instr       # live identity NOT frozen in
    assert "tool-discovery" in instr                   # lazy/deferred tool-loading fallback


# ----------------- skills layer -----------------
@pytest.mark.asyncio
async def test_write_and_get_skill():
    res = await call("write_skill", {
        "name": "deploy", "description": "How to deploy",
        "when_to_use": "deploying the app", "body": "1. build the image\n2. ship it to prod"})
    assert "Saved skill 'deploy' (v1)" in res[0].text

    got = await call("get_skill", {"name": "deploy"})
    assert "build the image" in got[0].text
    assert "deploying the app" in got[0].text


@pytest.mark.asyncio
async def test_write_skill_increments_version():
    await call("write_skill", {"name": "deploy", "description": "d",
                               "when_to_use": "w", "body": "version one body text"})
    res = await call("write_skill", {"name": "deploy", "description": "d",
                                     "when_to_use": "w", "body": "version two body text"})
    assert "v2" in res[0].text


@pytest.mark.asyncio
async def test_list_skills_catalog_no_body():
    await call("write_skill", {"name": "alpha", "description": "d",
                               "when_to_use": "do alpha", "body": "SECRET BODY content here long"})
    res = await call("list_skills", {})
    assert "alpha" in res[0].text
    assert "do alpha" in res[0].text
    assert "SECRET BODY" not in res[0].text


@pytest.mark.asyncio
async def test_unparseable_skill_is_named_rather_than_raised():
    """A skill file is hand-editable, so its frontmatter can be invalid YAML — here a
    colon inside an unquoted value. get_skill fed that straight to the parser and the
    ScannerError reached the client, naming neither the file nor a way out. It has no
    catalog line either, so list_skills is the only place it can be noticed at all."""
    config.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    (config.SKILLS_DIR / "broken.md").write_text(
        "---\nname: broken\ndescription: A skill: with a colon\nwhen_to_use: w\n---\nbody",
        encoding="utf-8")
    got = await call("get_skill", {"name": "broken"})
    assert "not valid YAML" in got[0].text, got[0].text
    assert "broken.md" in got[0].text and "write_skill" in got[0].text, got[0].text
    listed = await call("list_skills", {})
    assert "broken.md" in listed[0].text, listed[0].text


@pytest.mark.asyncio
async def test_delete_skill_removes_it_from_the_catalog():
    await call("write_skill", {"name": "obsolete", "description": "d",
                               "when_to_use": "never again", "body": "a body long enough to pass"})
    assert "obsolete" in (await call("list_skills", {}))[0].text

    res = await call("delete_skill", {"name": "obsolete"})
    assert "Deleted skill 'obsolete'" in res[0].text, res[0].text
    assert not (config.SKILLS_DIR / "obsolete.md").exists()
    assert "obsolete" not in (await call("list_skills", {}))[0].text
    assert "not found" in (await call("get_skill", {"name": "obsolete"}))[0].text


@pytest.mark.asyncio
async def test_a_deleted_skill_is_gone_from_the_pushed_context():
    """The catalog is not a listing the client asks for — get_core_context pushes it into
    EVERY session as the server's own instructions, so a skill nobody can retire keeps
    telling the model to follow a procedure that no longer applies. Removing it from the
    catalog is the whole point of the tool; the file being gone is only the mechanism."""
    await call("write_skill", {"name": "retired-loop", "description": "d",
                               "when_to_use": "RETIRED_TRIGGER_MARKER",
                               "body": "a body long enough to pass"})
    assert "RETIRED_TRIGGER_MARKER" in skills.build_prompt()

    await call("delete_skill", {"name": "retired-loop"})
    assert "RETIRED_TRIGGER_MARKER" not in skills.build_prompt()
    assert "retired-loop" not in skills.build_prompt()


@pytest.mark.asyncio
async def test_delete_skill_refuses_a_seed_the_repo_still_ships(tmp_path, monkeypatch):
    """Deleting a shipped seed would report success and then undo itself: seed_skills()
    reinstalls whatever is missing on the next start. Refusing says so instead."""
    seed_dir = tmp_path / "seed-skills"
    seed_dir.mkdir()
    _write_seed(seed_dir, "still-shipped.md", "a body long enough to pass")
    monkeypatch.setattr(config, "SEED_SKILLS_DIR", seed_dir)
    skills.seed_skills()
    installed = config.SKILLS_DIR / "still-shipped.md"
    assert installed.exists()

    res = await call("delete_skill", {"name": "still-shipped"})
    assert "rejected" in res[0].text.lower(), res[0].text
    assert "reinstalled" in res[0].text, res[0].text
    assert installed.exists(), "a shipped seed must survive the refusal"

    # Proof that the refusal is not cosmetic: seeding would bring it back anyway.
    installed.unlink()
    skills.seed_skills()
    assert installed.exists()


@pytest.mark.asyncio
async def test_delete_skill_retires_a_seed_the_repo_has_dropped(tmp_path, monkeypatch):
    """The case that motivates the tool. A renamed seed leaves its old file behind —
    seed_skills() iterates the SHIPPED seeds only, so nothing removes it, and the
    catalog advertises both. It is a managed seed, not user-authored, yet only a
    hand-edit in the vault could retire it before."""
    seed_dir = tmp_path / "seed-skills"
    seed_dir.mkdir()
    _write_seed(seed_dir, "old-name.md", "a body long enough to pass")
    monkeypatch.setattr(config, "SEED_SKILLS_DIR", seed_dir)
    skills.seed_skills()
    orphan = config.SKILLS_DIR / "old-name.md"
    assert skills.SEED_HASH_KEY in orphan.read_text(encoding="utf-8"), "expected a managed seed"

    # The rename: the repo ships the new name, and stops shipping the old one.
    (seed_dir / "old-name.md").unlink()
    _write_seed(seed_dir, "new-name.md", "a body long enough to pass")
    skills.seed_skills()
    assert orphan.exists(), "seeding leaves the orphan — that is what delete_skill is for"

    res = await call("delete_skill", {"name": "old-name"})
    assert "Deleted skill" in res[0].text, res[0].text
    assert not orphan.exists()
    names = {s["name"] for s in skills.list_skill_meta()}
    assert names == {"new-name"}


@pytest.mark.asyncio
async def test_delete_skill_does_not_take_a_glob():
    """The name is resolved to a literal path, so a pattern must miss rather than
    delete whatever it happens to match — a lost skill is a lost procedure."""
    await call("write_skill", {"name": "keeper", "description": "d", "when_to_use": "w",
                               "body": "a body long enough to pass"})
    res = await call("delete_skill", {"name": "keep*"})
    assert "not found" in res[0].text, res[0].text
    assert (config.SKILLS_DIR / "keeper.md").exists()


@pytest.mark.asyncio
async def test_delete_skill_traversal_blocked(tmp_path):
    outside = tmp_path / "victim.md"
    outside.write_text("---\nname: victim\n---\nbody", encoding="utf-8")
    res = await call("delete_skill", {"name": f"../{outside.name}"})
    assert "Security Error" in res[0].text or "not found" in res[0].text, res[0].text
    assert outside.exists()


@pytest.mark.asyncio
async def test_get_skill_traversal_blocked():
    res = await call("get_skill", {"name": "../../etc/passwd"})
    # Either a security error string or a clean not-found — never an escape.
    assert "Security Error" in res[0].text or "not found" in res[0].text


# ----------------- Task 3: seeded skills -----------------
@pytest.mark.asyncio
async def test_every_shipped_seed_is_retrievable_under_the_name_it_advertises():
    """A skill carries two names: the `name:` the catalog advertises and the filename
    get_skill resolves (SKILLS_DIR / f"{name}.md"). They are set in different places,
    so a rename can change one and leave the other — and the failure is invisible from
    the repo: the catalog names a skill the client then cannot load. Checked against
    the SHIPPED seeds, not a fixture, because that is where the two can drift apart."""
    skills.seed_skills()

    catalog = {s["name"] for s in skills.list_skill_meta()}
    # Tied to the shipped files, so a seed that fails to install cannot make this
    # vacuous — an empty or short catalog would otherwise pass every assertion below.
    assert len(catalog) == len(list(config.SEED_SKILLS_DIR.glob("*.md")))
    for name in sorted(catalog):
        got = (await call("get_skill", {"name": name}))[0].text
        assert "not found" not in got, f"the catalog advertises '{name}', get_skill cannot load it"


@pytest.mark.asyncio
async def test_seed_skills_installs_and_is_retrievable(tmp_path, monkeypatch):
    seed_dir = tmp_path / "seed-skills"
    seed_dir.mkdir()
    (seed_dir / "fake-seed.md").write_text(
        "---\nname: fake-seed\ndescription: d\nwhen_to_use: when seeding\nversion: 1\n---\n"
        "SEEDED_BODY_MARKER step one and two",
        encoding="utf-8")
    monkeypatch.setattr(config, "SEED_SKILLS_DIR", seed_dir)

    skills.seed_skills()

    names = {s["name"] for s in skills.list_skill_meta()}
    assert "fake-seed" in names
    got = await call("get_skill", {"name": "fake-seed"})
    assert "SEEDED_BODY_MARKER" in got[0].text


def test_seed_skills_does_not_overwrite_existing(tmp_path, monkeypatch):
    seed_dir = tmp_path / "seed-skills"
    seed_dir.mkdir()
    (seed_dir / "fake-seed.md").write_text(
        "---\nname: fake-seed\nversion: 1\n---\nSEED VERSION body text long enough",
        encoding="utf-8")
    monkeypatch.setattr(config, "SEED_SKILLS_DIR", seed_dir)
    # A user-authored skill of the same name already exists in SKILLS_DIR.
    (config.SKILLS_DIR / "fake-seed.md").write_text(
        "---\nname: fake-seed\nversion: 9\n---\nUSER VERSION body text long enough",
        encoding="utf-8")

    skills.seed_skills()

    body = (config.SKILLS_DIR / "fake-seed.md").read_text(encoding="utf-8")
    assert "USER VERSION" in body and "SEED VERSION" not in body


def _write_seed(seed_dir, name, body, version=1):
    (seed_dir / name).write_text(
        f"---\nname: {name[:-3]}\ndescription: d\nwhen_to_use: w\nversion: {version}\n---\n{body}",
        encoding="utf-8")


def test_seed_skills_updates_pristine_seed(tmp_path, monkeypatch):
    """A managed seed the user never touched is UPDATED to the shipped version."""
    seed_dir = tmp_path / "seed-skills"
    seed_dir.mkdir()
    _write_seed(seed_dir, "s.md", "ORIGINAL body long enough to pass")
    monkeypatch.setattr(config, "SEED_SKILLS_DIR", seed_dir)

    skills.seed_skills()  # installs stamped v1
    installed = (config.SKILLS_DIR / "s.md").read_text(encoding="utf-8")
    assert skills.SEED_HASH_KEY in installed and "ORIGINAL" in installed

    # Ship a new version of the same seed, restart-seed again.
    _write_seed(seed_dir, "s.md", "UPDATED body long enough to pass")
    skills.seed_skills()

    body = (config.SKILLS_DIR / "s.md").read_text(encoding="utf-8")
    assert "UPDATED" in body and "ORIGINAL" not in body


def test_seed_skills_keeps_edited_managed_seed(tmp_path, monkeypatch):
    """Once the user edits a managed seed, a newer shipped version does NOT clobber it."""
    seed_dir = tmp_path / "seed-skills"
    seed_dir.mkdir()
    _write_seed(seed_dir, "s.md", "ORIGINAL body long enough to pass")
    monkeypatch.setattr(config, "SEED_SKILLS_DIR", seed_dir)
    skills.seed_skills()  # installs stamped v1

    # User edits the installed (managed) seed in place, keeping the marker.
    dest = config.SKILLS_DIR / "s.md"
    edited = dest.read_text(encoding="utf-8").replace("ORIGINAL", "USER EDITED")
    dest.write_text(edited, encoding="utf-8")

    # A newer version ships; seeding must leave the user's edit alone.
    _write_seed(seed_dir, "s.md", "UPDATED body long enough to pass")
    skills.seed_skills()

    body = dest.read_text(encoding="utf-8")
    assert "USER EDITED" in body and "UPDATED" not in body


def test_seed_skills_adopts_pristine_unmarked_seed(tmp_path, monkeypatch):
    """An unmarked file identical to the shipped seed (pre-managed install) is adopted
    into management and updated; an unmarked DIVERGENT file is left untouched."""
    seed_dir = tmp_path / "seed-skills"
    seed_dir.mkdir()
    _write_seed(seed_dir, "s.md", "SHIPPED body long enough to pass")
    monkeypatch.setattr(config, "SEED_SKILLS_DIR", seed_dir)

    # Simulate a pre-managed install: the exact shipped content, but NO marker.
    (config.SKILLS_DIR / "s.md").write_text(
        (seed_dir / "s.md").read_text(encoding="utf-8"), encoding="utf-8")

    skills.seed_skills()
    body = (config.SKILLS_DIR / "s.md").read_text(encoding="utf-8")
    assert skills.SEED_HASH_KEY in body  # adopted into management


def test_seed_skills_adopts_unmarked_seed_that_only_lags_in_version(tmp_path, monkeypatch):
    """A version bump alone must not lock a pristine unmarked file out of management.

    `version` is bumped in the repo, never by the user, so a file identical apart from
    it is still untouched — and the divergence would never heal, so without this the
    file would silently miss every future seed update.
    """
    seed_dir = tmp_path / "seed-skills"
    seed_dir.mkdir()
    monkeypatch.setattr(config, "SEED_SKILLS_DIR", seed_dir)

    # A pre-managed install of v1: no marker, and the repo has since shipped v2.
    _write_seed(seed_dir, "s.md", "SHIPPED body long enough to pass", version=1)
    (config.SKILLS_DIR / "s.md").write_text(
        (seed_dir / "s.md").read_text(encoding="utf-8"), encoding="utf-8")
    _write_seed(seed_dir, "s.md", "SHIPPED body long enough to pass", version=2)

    skills.seed_skills()

    body = (config.SKILLS_DIR / "s.md").read_text(encoding="utf-8")
    assert skills.SEED_HASH_KEY in body  # adopted into management
    assert "version: 2" in body          # and brought up to the shipped version


def test_seed_skills_leaves_unmarked_seed_whose_catalog_text_was_edited(tmp_path, monkeypatch):
    """Ignoring `version` must not widen adoption to metadata the user does edit:
    `description` and `when_to_use` are the catalog text and stay user-owned."""
    seed_dir = tmp_path / "seed-skills"
    seed_dir.mkdir()
    monkeypatch.setattr(config, "SEED_SKILLS_DIR", seed_dir)

    _write_seed(seed_dir, "s.md", "SHIPPED body long enough to pass", version=1)
    (config.SKILLS_DIR / "s.md").write_text(
        (seed_dir / "s.md").read_text(encoding="utf-8").replace(
            "description: d", "description: my own wording"), encoding="utf-8")
    _write_seed(seed_dir, "s.md", "SHIPPED body long enough to pass", version=2)

    skills.seed_skills()

    body = (config.SKILLS_DIR / "s.md").read_text(encoding="utf-8")
    assert skills.SEED_HASH_KEY not in body
    assert "my own wording" in body and "version: 1" in body
