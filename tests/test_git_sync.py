"""sync_git: every path a sync can take, and what it leaves behind.

A conflict parks HEAD instead of resolving it, a rejected push does not count as
success, local-only mode returns after the commit, and the vault ignore list keeps
Obsidian's local state out of the mirror.
"""
import logging

import pytest
from git import PushInfo as _PushInfo

import config
import git_sync
import observability
from conftest import call, _counter_value



class _FakePushInfo:
    """The PushInfo surface _push reads: the flags bitmask and the flag constants it
    is tested against, borrowed from the real class so the fake cannot disagree with
    production about which bit means rejected."""
    REJECTED = _PushInfo.REJECTED
    REMOTE_REJECTED = _PushInfo.REMOTE_REJECTED
    ERROR = _PushInfo.ERROR

    def __init__(self, flags=_PushInfo.FAST_FORWARD, summary="  main -> main"):
        self.flags = flags
        self.summary = summary


def _fake_repo(*, remotes=True, pull_raises=None, push_raises=None, commit_raises=None,
               push_rejected=False, git_dir=None):
    """A stand-in exposing exactly the repo surface sync_git touches.

    git_dir is where _rebase_in_progress looks for the rebase state directory, so a
    test decides whether a failed pull counts as a real conflict by creating
    rebase-merge in it. push_rejected returns a rejected PushInfo instead of raising,
    which is how GitPython reports a refused push.

    Returns (repo, calls); calls counts add/commit/pull/push, records the conflict
    branch name and whether the push set an upstream, so a test can assert what
    sync_git actually did.
    """
    calls = {"add": 0, "commit": 0, "pull": 0, "push": 0, "branch": None,
             "set_upstream": False}

    class FakeGit:
        def add(self, all=False): calls["add"] += 1

        def pull(self, **k):
            calls["pull"] += 1
            if pull_raises:
                raise pull_raises

        def rebase(self, *a): pass
        def branch(self, name): calls["branch"] = name

    class FakeIndex:
        def commit(self, message):
            calls["commit"] += 1
            if commit_raises:
                raise commit_raises

    class FakeOrigin:
        @staticmethod
        def push(**kwargs):
            calls["push"] += 1
            calls["set_upstream"] = bool(kwargs.get("set_upstream"))
            if push_raises:
                raise push_raises
            if push_rejected:
                return [_FakePushInfo(_PushInfo.REJECTED, "! [rejected] main -> main")]
            return [_FakePushInfo()]

    class FakeRepo:
        def __init__(self):
            self.git = FakeGit()
            self.index = FakeIndex()
            self.git_dir = str(git_dir) if git_dir else ""
            self.active_branch = type("B", (), {"name": "main"})()
            # An empty tuple is falsy, which is how sync_git detects local-only mode.
            self.remotes = type("R", (), {"origin": FakeOrigin})() if remotes else ()

        def is_dirty(self): return True

        @property
        def untracked_files(self): return []

    return FakeRepo(), calls


def _real_vault_repo(monkeypatch, tmp_path):
    """A real git repo on the vault root, opened through init_git_repo.

    The ignore list is only worth anything if git honours it, and git's ignore
    semantics are git's — so these tests use a real repo and ask `git ls-files`
    what actually got staged, rather than re-implementing the matching.
    """
    from git import Repo
    Repo.init(tmp_path)
    monkeypatch.setattr(config, "GIT_REPO_URL", None)   # local-only: no push
    monkeypatch.setattr(git_sync, "repo", None)         # restored after the test
    monkeypatch.setattr(git_sync, "_sync_paused", False)
    git_sync.init_git_repo()
    return git_sync.repo


def test_git_sync_keeps_obsidian_state_and_trash_out_of_the_mirror(monkeypatch, tmp_path):
    """sync_git stages the whole vault, so without an ignore list an Obsidian user's
    workspace layout, plugin data and deleted notes reach the remote within one cron
    cycle. Notes still have to go through, or the ignore list has eaten the product."""
    repo = _real_vault_repo(monkeypatch, tmp_path)
    (config.BRAIN_DIR / "standard").mkdir(parents=True, exist_ok=True)
    (config.BRAIN_DIR / "standard" / "keep.md").write_text("# Keep\n", encoding="utf-8")
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".obsidian" / "plugins").mkdir()
    (tmp_path / ".obsidian" / "plugins" / "data.json").write_text('{"apiKey":"x"}',
                                                                  encoding="utf-8")
    (tmp_path / ".trash").mkdir()
    (tmp_path / ".trash" / "deleted.md").write_text("# Gone\n", encoding="utf-8")
    (config.BRAIN_DIR / "scratch.tmp").write_text("x", encoding="utf-8")

    assert git_sync.sync_git("test seed") is True
    committed = set(repo.git.ls_files().splitlines())
    assert "brain-vault/standard/keep.md" in committed
    assert ".gitignore" in committed          # the list travels with the mirror
    leaked = {f for f in committed
              if f.startswith((".obsidian/", ".trash/")) or f.endswith(".tmp")}
    assert not leaked, f"these reached the mirror: {sorted(leaked)}"


def test_an_existing_vault_gitignore_is_never_overwritten(monkeypatch, tmp_path):
    """The file is the operator's — someone who wants their Obsidian setup mirrored
    across devices deletes that line, and a server restart must not put it back."""
    own = "# mine\n*.secret\n"
    (tmp_path / ".gitignore").write_text(own, encoding="utf-8")
    _real_vault_repo(monkeypatch, tmp_path)
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == own


def test_already_tracked_ignored_files_are_reported_not_untracked(monkeypatch, tmp_path,
                                                                 caplog):
    """A vault that has been mirroring .obsidian/ keeps doing so: `git rm --cached`
    here would delete it on every other device that pulls. Naming the files is the
    most that can be done without risking someone's data."""
    from git import Repo
    seeded = Repo.init(tmp_path)
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8")
    seeded.git.add(all=True)
    seeded.config_writer().set_value("user", "email", "t@t").release()
    seeded.config_writer().set_value("user", "name", "t").release()
    seeded.index.commit("pre-existing obsidian state")

    with caplog.at_level(logging.WARNING):
        repo = _real_vault_repo(monkeypatch, tmp_path)
    assert ".obsidian/workspace.json" in repo.git.ls_files()   # still tracked
    assert "workspace.json" in caplog.text and "git rm" in caplog.text


def test_sync_git_conflict_parks_and_bails(monkeypatch, tmp_path):
    from git import exc as git_exc

    # A real divergence leaves git mid-rebase; that state dir is what tells the
    # conflict apart from a pull that never got as far as rebasing.
    (tmp_path / "rebase-merge").mkdir()
    repo, calls = _fake_repo(pull_raises=git_exc.GitCommandError("pull", 1),
                             git_dir=tmp_path)
    monkeypatch.setattr(git_sync, "repo", repo)
    monkeypatch.setattr(git_sync, "_sync_paused", False)  # auto-restored after test
    result = git_sync.sync_git("test conflict")
    assert result is False                 # bailed cleanly, did not wedge
    assert calls["branch"] is not None     # local work parked on a conflict branch
    assert calls["push"] == 0              # never pushed a diverged branch
    assert git_sync._sync_paused is True   # latch set so the next sync is skipped


def test_sync_git_first_sync_to_an_empty_remote_publishes_the_branch(monkeypatch, tmp_path):
    """A brand-new private repo has no branch to merge with, so the very first pull
    fails. That is not a conflict: it must push (setting the upstream) instead of
    parking the vault on a conflict branch and pausing sync before it ever ran."""
    from git import exc as git_exc

    repo, calls = _fake_repo(pull_raises=git_exc.GitCommandError("pull", 1),
                             git_dir=tmp_path)  # no rebase-merge: never started rebasing
    monkeypatch.setattr(git_sync, "repo", repo)
    monkeypatch.setattr(git_sync, "_sync_paused", False)
    label = 'mcp_git_sync_total{result="ok"}'
    before = _counter_value(observability.metrics.render(), label)
    result = git_sync.sync_git("first sync")
    assert result is True
    assert calls["push"] == 1 and calls["set_upstream"] is True
    assert calls["branch"] is None              # nothing to park
    assert git_sync.is_sync_paused() is False   # sync stays alive
    assert _counter_value(observability.metrics.render(), label) == before + 1


def test_sync_git_unreachable_remote_recovers_on_the_next_sync(monkeypatch, tmp_path):
    """An outage or a rotated deploy key fails the pull the same way an empty remote
    does. Pausing on it would disable the mirror until a human called resume_sync, so
    the failure has to stay retryable — the next sync must go through on its own."""
    from git import exc as git_exc

    offline, calls = _fake_repo(pull_raises=git_exc.GitCommandError("pull", 1),
                                push_raises=git_exc.GitCommandError("push", 1),
                                git_dir=tmp_path)
    monkeypatch.setattr(git_sync, "repo", offline)
    monkeypatch.setattr(git_sync, "_sync_paused", False)
    label = 'mcp_git_sync_total{result="push_failed"}'
    before = _counter_value(observability.metrics.render(), label)
    assert git_sync.sync_git("during outage") is False
    assert calls["branch"] is None
    assert git_sync.is_sync_paused() is False
    assert _counter_value(observability.metrics.render(), label) == before + 1

    # Remote is back: no human intervention needed.
    healthy, calls2 = _fake_repo(git_dir=tmp_path)
    monkeypatch.setattr(git_sync, "repo", healthy)
    assert git_sync.sync_git("after recovery") is True
    assert calls2["push"] == 1


def test_sync_git_rejected_push_is_not_reported_as_success(monkeypatch, tmp_path):
    """GitPython reports a refused push as a rejected PushInfo, not an exception, so
    an exception-only check would count a sync as mirrored while the remote never
    received it."""
    repo, calls = _fake_repo(push_rejected=True, git_dir=tmp_path)
    monkeypatch.setattr(git_sync, "repo", repo)
    monkeypatch.setattr(git_sync, "_sync_paused", False)
    labels = {k: f'mcp_git_sync_total{{result="{k}"}}'
              for k in ("push_failed", "ok", "error")}
    before = {k: _counter_value(observability.metrics.render(), v) for k, v in labels.items()}
    result = git_sync.sync_git("test rejected push")
    after = {k: _counter_value(observability.metrics.render(), v) for k, v in labels.items()}
    assert result is False
    assert after["push_failed"] == before["push_failed"] + 1
    assert after["ok"] == before["ok"]        # not counted as mirrored
    assert after["error"] == before["error"]  # a rejection is not an internal error
    assert git_sync.is_sync_paused() is False


def test_sync_git_paused_skips_without_touching_repo(monkeypatch):
    """Once paused after a conflict, further syncs must be a no-op (no new branches)."""
    import git_sync

    touched = {"add": 0, "branch": 0}

    class FakeGit:
        def add(self, all=False): touched["add"] += 1
        def branch(self, name): touched["branch"] += 1

    class FakeRepo:
        git = FakeGit()

    monkeypatch.setattr(git_sync, "repo", FakeRepo())
    monkeypatch.setattr(git_sync, "_sync_paused", True)
    assert git_sync.sync_git("should skip") is False
    assert touched == {"add": 0, "branch": 0}  # never reached the repo


def test_sync_git_not_configured_logs_and_increments_metric(monkeypatch):
    """repo is None (init_git_repo failed or was never called) must be visible in
    /metrics, not a silent no-op."""
    import git_sync
    monkeypatch.setattr(git_sync, "repo", None)
    label = 'mcp_git_sync_total{result="not_configured"}'
    before = _counter_value(observability.metrics.render(), label)
    result = git_sync.sync_git("test message")
    after = _counter_value(observability.metrics.render(), label)
    assert result is False
    assert after == before + 1


def test_sync_git_pushes_and_reports_ok(monkeypatch):
    """The success path: a clean sync commits, pulls, pushes and counts result=ok."""
    repo, calls = _fake_repo()
    monkeypatch.setattr(git_sync, "repo", repo)
    monkeypatch.setattr(git_sync, "_sync_paused", False)
    label = 'mcp_git_sync_total{result="ok"}'
    before = _counter_value(observability.metrics.render(), label)
    result = git_sync.sync_git("test ok")
    assert result is True
    assert calls["commit"] == 1 and calls["push"] == 1
    assert _counter_value(observability.metrics.render(), label) == before + 1


def test_sync_git_push_failure_is_retryable(monkeypatch):
    """A failed push must report False so the caller doesn't treat the work as
    mirrored — but it must NOT set the conflict latch, since a retry can succeed."""
    from git import exc as git_exc

    repo, calls = _fake_repo(push_raises=git_exc.GitCommandError("push", 1))
    monkeypatch.setattr(git_sync, "repo", repo)
    monkeypatch.setattr(git_sync, "_sync_paused", False)
    label = 'mcp_git_sync_total{result="push_failed"}'
    before = _counter_value(observability.metrics.render(), label)
    result = git_sync.sync_git("test push failure")
    assert result is False
    assert calls["push"] == 1                  # it was attempted
    assert git_sync.is_sync_paused() is False  # retryable, unlike a conflict
    assert _counter_value(observability.metrics.render(), label) == before + 1


def test_sync_git_local_only_succeeds_without_pushing(monkeypatch):
    """Without a remote the commit alone IS success — the one path where a True
    return does not mean the work reached a remote."""
    repo, calls = _fake_repo(remotes=False)
    monkeypatch.setattr(git_sync, "repo", repo)
    monkeypatch.setattr(git_sync, "_sync_paused", False)
    label = 'mcp_git_sync_total{result="local_only"}'
    before = _counter_value(observability.metrics.render(), label)
    result = git_sync.sync_git("test local only")
    assert result is True
    assert calls["commit"] == 1
    assert calls["pull"] == 0 and calls["push"] == 0
    assert _counter_value(observability.metrics.render(), label) == before + 1


def test_sync_git_unexpected_error_returns_false(monkeypatch):
    """An unexpected exception must degrade to False, not escape into git_worker."""
    repo, calls = _fake_repo(commit_raises=RuntimeError("boom"))
    monkeypatch.setattr(git_sync, "repo", repo)
    monkeypatch.setattr(git_sync, "_sync_paused", False)
    label = 'mcp_git_sync_total{result="error"}'
    before = _counter_value(observability.metrics.render(), label)
    result = git_sync.sync_git("test error")
    assert result is False
    assert calls["push"] == 0
    assert _counter_value(observability.metrics.render(), label) == before + 1


def test_resume_sync_clears_pause(monkeypatch):
    import git_sync
    monkeypatch.setattr(git_sync, "_sync_paused", True)
    git_sync.resume_sync()
    assert git_sync._sync_paused is False


@pytest.mark.asyncio
async def test_resume_sync_tool_clears_pause(monkeypatch):
    """The MCP tool (the ONLY runtime path to un-pause) must clear the latch."""
    import git_sync
    monkeypatch.setattr(git_sync, "_sync_paused", True)
    res = await call("resume_sync", {})
    assert "resumed" in res[0].text.lower()
    assert git_sync._sync_paused is False


@pytest.mark.asyncio
async def test_resume_sync_tool_noop_when_active(monkeypatch):
    import git_sync
    monkeypatch.setattr(git_sync, "_sync_paused", False)
    res = await call("resume_sync", {})
    assert "not paused" in res[0].text.lower()


@pytest.mark.asyncio
async def test_sync_status_reports_paused(monkeypatch):
    import git_sync
    monkeypatch.setattr(git_sync, "repo", object())   # pretend a repo is configured
    monkeypatch.setattr(git_sync, "_sync_paused", True)
    res = await call("sync_status", {})
    assert "paused" in res[0].text.lower()
    monkeypatch.setattr(git_sync, "_sync_paused", False)
    res = await call("sync_status", {})
    assert "active" in res[0].text.lower()
