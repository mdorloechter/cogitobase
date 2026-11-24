"""Issue #16: skills flagged `slash: true` are exposed as MCP prompts.

A client that lists prompts shows them as slash commands (Claude Code:
/mcp__<server>__<skill>), so the user can start a skill directly. The flag is opt-in
per skill; the catalog push and get_skill pull (ADR 3/14) are unchanged, and a
slash command is an explicit pull, so a body still never enters a session unasked.
"""
from pathlib import Path

import frontmatter
import pytest

import config
import server
import skills
from conftest import call

_REPO = Path(__file__).resolve().parent.parent


def _skill(name, body="Step one: do the thing.\nStep two: report.", *, slash=None,
           when="when it is morning", version=1):
    meta = {"name": name, "description": f"{name} procedure", "when_to_use": when,
            "version": version}
    if slash is not None:
        meta["slash"] = slash
    config.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.SKILLS_DIR / f"{name}.md"
    path.write_text(frontmatter.dumps(frontmatter.Post(body, **meta)), encoding="utf-8")
    return path


def _prompt_text(result) -> str:
    return "\n".join(m.content.text for m in result.messages)


# ----------------- list_prompts -----------------

@pytest.mark.asyncio
async def test_only_flagged_skills_are_listed_next_to_the_meta_prompt():
    _skill("morgenroutine", slash=True, when="Wenn Marius die Morgenroutine startet")
    _skill("plain")                       # no flag
    _skill("off", slash=False)
    prompts = {p.name: p for p in await server.list_prompts()}
    assert set(prompts) == {skills.PROMPT_NAME, "morgenroutine"}
    assert prompts["morgenroutine"].description == "Wenn Marius die Morgenroutine startet"


@pytest.mark.asyncio
async def test_a_skill_prompt_takes_one_optional_free_text_argument():
    _skill("morgenroutine", slash=True)
    prompt = next(p for p in await server.list_prompts() if p.name == "morgenroutine")
    assert [(a.name, a.required) for a in prompt.arguments] == [("context", False)]


@pytest.mark.asyncio
async def test_a_symlinked_or_unreadable_skill_never_becomes_a_prompt(tmp_path):
    """Same containment rule as the catalog: a symlink escaping the skills dir would
    be disclosed and run as instructions."""
    outside = tmp_path / "outside.md"
    outside.write_text("---\nname: evil\ndescription: d\nwhen_to_use: w\nslash: true\n"
                       "version: 1\n---\nOUTSIDE body text here", encoding="utf-8")
    config.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    (config.SKILLS_DIR / "evil.md").symlink_to(outside)
    (config.SKILLS_DIR / "broken.md").write_text("---\nname: [unclosed\n---\nbody",
                                                 encoding="utf-8")
    names = {p.name for p in await server.list_prompts()}
    assert names == {skills.PROMPT_NAME}


# ----------------- get_prompt -----------------

@pytest.mark.asyncio
async def test_get_prompt_returns_the_current_body_with_a_run_header():
    _skill("session-capture", "Capture the retro now, then the learnings.", slash=True,
           version=4)
    text = _prompt_text(await server.get_prompt("session-capture", None))
    assert "Capture the retro now, then the learnings." in text
    assert "session-capture" in text and "v4" in text
    assert "get_core_context" in text           # bootstrap if the session has not


@pytest.mark.asyncio
async def test_the_free_text_argument_is_passed_on():
    _skill("session-capture", slash=True)
    text = _prompt_text(await server.get_prompt("session-capture",
                                                {"context": "nur cogitobase"}))
    assert "nur cogitobase" in text


@pytest.mark.asyncio
async def test_a_skill_update_reaches_the_prompt_without_a_restart():
    _skill("morgenroutine", "OLD procedure body text", slash=True)
    await call("write_skill", {"name": "morgenroutine", "description": "d",
                               "when_to_use": "w", "body": "NEW procedure body text"})
    text = _prompt_text(await server.get_prompt("morgenroutine", None))
    assert "NEW procedure body text" in text and "OLD" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["unknown", "plain"])
async def test_an_unknown_or_unflagged_name_is_an_error(name):
    _skill("plain")
    with pytest.raises(ValueError):
        await server.get_prompt(name, None)


@pytest.mark.asyncio
async def test_the_meta_prompt_still_works():
    result = await server.get_prompt(skills.PROMPT_NAME, None)
    assert "AVAILABLE SKILLS" in _prompt_text(result)


# ----------------- write_skill keeps the flag -----------------

def _flag(name):
    return frontmatter.load(config.SKILLS_DIR / f"{name}.md").metadata.get("slash")


_ARGS = {"description": "d", "when_to_use": "w", "body": "A procedure body long enough."}


@pytest.mark.asyncio
async def test_write_skill_sets_and_clears_the_flag():
    await call("write_skill", {"name": "s", **_ARGS, "slash": True})
    assert _flag("s") is True
    await call("write_skill", {"name": "s", **_ARGS, "slash": False})
    assert not _flag("s")


@pytest.mark.asyncio
async def test_a_body_update_without_the_parameter_keeps_the_command():
    """Overwriting a skill to fix its body must not silently remove its slash command."""
    _skill("morgenroutine", slash=True)
    await call("write_skill", {"name": "morgenroutine", **_ARGS})
    assert _flag("morgenroutine") is True


@pytest.mark.asyncio
async def test_a_new_skill_without_the_parameter_has_no_command():
    await call("write_skill", {"name": "fresh", **_ARGS})
    assert not _flag("fresh")


# ----------------- the shipped seed -----------------

def test_the_session_capture_seed_ships_as_a_slash_command():
    meta = frontmatter.load(_REPO / "seed-skills" / "session-capture.md").metadata
    assert meta.get("slash") is True


def test_a_pristine_seed_picks_up_the_flag_on_the_next_start(tmp_path, monkeypatch):
    """The vault copy of session-capture is a pristine managed seed, so shipping the
    flag in the repo is enough: the next start updates it."""
    seed_dir = tmp_path / "seeds"
    seed_dir.mkdir()
    monkeypatch.setattr(config, "SEED_SKILLS_DIR", seed_dir)
    shipped = seed_dir / "capture.md"
    shipped.write_text("---\nname: capture\ndescription: d\nwhen_to_use: w\nversion: 1\n---\n"
                       "Capture what the session produced.", encoding="utf-8")
    skills.seed_skills()
    assert not _flag("capture")

    shipped.write_text("---\nname: capture\ndescription: d\nwhen_to_use: w\nversion: 2\n"
                       "slash: true\n---\nCapture what the session produced.", encoding="utf-8")
    skills.seed_skills()
    assert _flag("capture") is True
