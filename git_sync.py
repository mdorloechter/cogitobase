"""Git synchronization: clone/open repo, sync (pull+commit+push), serialized worker queue.

ADR 4: the markdown vault is the single source of truth and every mutating
operation is mirrored to the private GitHub repo. All git operations are
serialized through one worker to avoid races.
"""
import asyncio
import os
import secrets
from pathlib import Path

from git import Repo, exc

import config
from config import log
from observability import metrics
from registry import register, text

# Git instruments.
_M_SYNC = metrics.counter("mcp_git_sync_total", "Git sync attempts by result.", ("result",))
_M_QUEUE = metrics.gauge("mcp_git_queue_depth", "Pending git sync jobs in the queue.")

repo = None

# Once a rebase conflict parks local work on a branch, auto-sync is PAUSED
# until a human resolves the divergence. Without this latch every subsequent sync
# would hit the same conflict and spawn yet another conflict-<hex> branch forever.
_sync_paused = False

# Authenticate git over SSH with a mounted deploy key, never a token in the URL —
# git echoes the remote URL in its own error output, which lands in the logs, so
# config.ensure_git_repo_url refuses a URL that carries one at startup.
# Honoured by GitPython because we export it into the process environment at init.
_SSH_KEY_PATH = os.environ.get("GIT_SSH_KEY", "/home/mcpuser/.ssh/id_ed25519")

git_queue: asyncio.Queue | None = None

# sync_git stages the whole vault (`git add -A`), so whatever an editor or an app
# drops next to the notes reaches the remote. This is the list that stops it.
# `.obsidian/` is excluded WHOLE: it holds per-machine workspace layout that is
# rewritten every time a pane moves, and plugin data that can carry an API key.
# An operator who wants their Obsidian setup mirrored across devices deletes that
# line — the file is theirs, and cogitobase never rewrites it.
_VAULT_GITIGNORE = """\
# Seeded by cogitobase, then left alone — adapt it to your vault.
# Everything not listed here is committed and pushed to your git remote.

# Obsidian's local state: workspace layout, hotkeys, themes, plugin data.
.obsidian/

# Obsidian moves deleted notes here. A note deleted on purpose must not travel to
# the remote and come back on the next pull.
.trash/

# Editor and OS scratch files.
*.tmp
~$*
.DS_Store
Thumbs.db
desktop.ini
"""


def _seed_vault_gitignore():
    """Give the vault an ignore list, and report tracked files it cannot cover.

    An existing file — from the clone, or adapted by the operator — is the
    authority and stays untouched. Ignoring a path does not untrack it, and
    untracking here would delete that path on every other device that pulls, so a
    vault already mirroring its Obsidian state keeps doing so until a human says
    otherwise. Naming the files is the most this can do without risking data.
    """
    path = config.VAULT_ROOT / ".gitignore"
    if not path.exists():
        try:
            path.write_text(_VAULT_GITIGNORE, encoding="utf-8")
            log.info("Seeded vault-data/.gitignore.",
                     extra={"event": "git_sync", "result": "gitignore_seeded"})
        except OSError:
            log.exception(
                "Could not write vault-data/.gitignore — the vault has no ignore list, "
                "so Obsidian's local state and .trash/ are committed and pushed with "
                "every sync.", extra={"event": "git_sync", "result": "gitignore_failed"})
            return
    try:
        tracked = repo.git.ls_files("-i", "-c", "--exclude-standard").splitlines()
    except Exception:
        return
    if tracked:
        log.warning(
            "%d tracked file(s) in vault-data match its .gitignore and keep reaching "
            "the remote: %s. Untracking them here would delete them on every other "
            "device that pulls, so run `git rm -r --cached <path>` in vault-data "
            "yourself once you are sure.",
            len(tracked), ", ".join(tracked[:10]) + (", …" if len(tracked) > 10 else ""),
            extra={"event": "git_sync", "result": "gitignore_tracked"})


def init_git_repo():
    global repo
    # If a deploy key is present, force git to use it for all transports.
    # Host-key policy: if the operator seeded ./ssh/known_hosts we require a match
    # (StrictHostKeyChecking=yes, no silent MITM on first connect); only fall back
    # to trust-on-first-use when no known_hosts was provided.
    if os.path.exists(_SSH_KEY_PATH) and "GIT_SSH_COMMAND" not in os.environ:
        known_hosts = os.path.join(os.path.dirname(_SSH_KEY_PATH), "known_hosts")
        strict = "yes" if os.path.exists(known_hosts) else "accept-new"
        cmd = f"ssh -i {_SSH_KEY_PATH} -o IdentitiesOnly=yes -o StrictHostKeyChecking={strict}"
        if os.path.exists(known_hosts):
            cmd += f" -o UserKnownHostsFile={known_hosts}"
        os.environ["GIT_SSH_COMMAND"] = cmd
    # Durability signal: make it unmistakable in the logs whether the vault is
    # mirrored to a remote or running local-only.
    if not config.GIT_REPO_URL:
        log.warning(
            "GIT_REPO_URL is not set — running LOCAL-ONLY: vault changes are committed "
            "locally but NOT pushed to any remote. The ADR-4 durability/backup guarantee "
            "is OFF until you configure a remote.",
            extra={"event": "git_sync", "result": "local_only"})
    # Decide on the presence of a .git dir, NOT on whether VAULT_ROOT exists:
    # the compose bind mount (./vault-data:/app/vault-data) always materialises
    # the directory, so an "exists?" check would never let the initial clone run
    # and every fresh install would silently fall through to "not a git repo".
    if (config.VAULT_ROOT / ".git").exists():
        # Already a working clone — just open it.
        try:
            repo = Repo(config.VAULT_ROOT)
        except exc.InvalidGitRepositoryError:
            log.warning("vault-data has a .git entry but is not a valid git repo; running without sync.")
            return
    elif config.GIT_REPO_URL:
        # No repo yet but a remote is configured → clone. git can clone into an
        # existing EMPTY directory (the bind-mounted vault-data), so only refuse
        # when the directory is non-empty (cloning there would error out).
        if config.VAULT_ROOT.exists() and any(config.VAULT_ROOT.iterdir()):
            log.warning(
                "vault-data is non-empty but has no .git — cannot clone into it. "
                "Running without sync. Remove/relocate the existing files or seed the "
                "clone manually to enable git sync.",
                extra={"event": "git_sync", "result": "clone_skipped_nonempty"})
            return
        try:
            # core.symlinks=false has to apply DURING the clone — the checkout
            # happens there, so the config written further down would come too late
            # to stop a symlink from materialising. Passed via GIT_CONFIG_* rather
            # than `--config`, which GitPython rejects as an unsafe clone option
            # (and allow_unsafe_options would lift that guard for every option).
            repo = Repo.clone_from(config.GIT_REPO_URL, config.VAULT_ROOT, env={
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.symlinks",
                "GIT_CONFIG_VALUE_0": "false",
            })
        except Exception:
            # State the CONSEQUENCE, not just the failure: the server keeps serving,
            # but vault-data never becomes a git repo, so nothing is committed and
            # nothing is pushed. Sync stays off until this is fixed and the server
            # restarted — the ADR-4 durability guarantee is not in effect.
            log.exception(
                "Initial git clone failed — vault-data is NOT a git repo: changes are "
                "neither committed nor pushed, and sync stays off until this is fixed "
                "and the server restarted. Check GIT_REPO_URL and the deploy key.",
                extra={"event": "git_sync", "result": "clone_failed"})
            return
        config.CONTEXT_DIR.mkdir(exist_ok=True, parents=True)
        config.BRAIN_DIR.mkdir(exist_ok=True, parents=True)
    else:
        # Local-only: no remote configured. Initialise a local repo anyway so
        # vault changes are still captured as real commits (recoverable history),
        # even though nothing is pushed. See the LOCAL-ONLY warning above.
        try:
            repo = Repo.init(config.VAULT_ROOT)
        except Exception:
            log.exception("Local git init failed.")
            return
        config.CONTEXT_DIR.mkdir(exist_ok=True, parents=True)
        config.BRAIN_DIR.mkdir(exist_ok=True, parents=True)
    if repo:
        # Before anything is staged, and for all three branches above: an existing
        # clone needs the list as much as a fresh one, and it is the older vaults
        # that already have Obsidian state in the tree.
        _seed_vault_gitignore()
        repo.config_writer().set_value("user", "name", config.GIT_USER_NAME).release()
        repo.config_writer().set_value("user", "email", config.GIT_USER_EMAIL).release()
        # Defence in depth for the vault-escape class: a symlink committed to the
        # mirror (a 120000 blob) would otherwise materialise on clone/pull and point
        # anywhere on the host. With this set, git writes a plain file holding the
        # target path instead, so the link never exists to be followed. The readers
        # guard themselves too (security.is_contained_file); this stops it earlier.
        # Matches the analyze_github_repo clone, which passes core.symlinks=false.
        repo.config_writer().set_value("core", "symlinks", "false").release()
        # Prefer a credential helper / SSH key configured outside the repo URL.
        # The token must NOT live in GIT_REPO_URL; if a helper is configured, git uses it.


def _rebase_in_progress() -> bool:
    """True while git sits mid-rebase, i.e. the pull hit a real divergence.

    This is what separates a conflict from a pull that failed before rebasing at
    all (empty or unreachable remote): all three raise GitCommandError, but only a
    conflict leaves the rebase state directory behind. Read from the local .git
    dir, so no network round-trip and nothing to be wrong about when it is down.
    """
    try:
        git_dir = Path(repo.git_dir)
    except Exception:
        return False
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


def _push(first_push: bool = False) -> bool:
    """Push the current branch, reporting whether the work reached the remote.

    first_push sets the upstream, which the initial push to an empty remote needs
    (nothing to track yet). A rejected push is NOT an exception in GitPython — it
    comes back as a PushInfo with the REJECTED/ERROR flag — so the flags decide
    the verdict; trusting the absence of an exception would report a sync as
    mirrored while the remote never received it.
    """
    try:
        branch = repo.active_branch.name
        if first_push:
            infos = repo.remotes.origin.push(refspec=f"{branch}:{branch}", set_upstream=True)
        else:
            infos = repo.remotes.origin.push()
    except exc.GitCommandError:
        log.warning("git push failed (non-fast-forward or transport); will retry next sync.")
        _M_SYNC.inc(("push_failed",))
        return False
    rejected = [i for i in infos if i.flags & (i.REJECTED | i.ERROR | i.REMOTE_REJECTED)]
    if rejected:
        log.warning("git push was rejected by the remote (%s); will retry next sync.",
                    "; ".join(str(i.summary).strip() for i in rejected))
        _M_SYNC.inc(("push_failed",))
        return False
    _M_SYNC.inc(("ok",))
    log.info("Git sync pushed", extra={"event": "git_sync", "result": "ok"})
    return True


def sync_git(message="Auto-sync from MCP Server"):
    """Commit local work, then pull (rebase) & push. Never discard commits,
    and never wedge the sync — on conflict, park work on a branch and bail cleanly."""
    global _sync_paused
    if not repo:
        log.warning("Git sync is not configured (no repo); skipping.",
                    extra={"event": "git_sync", "result": "not_configured"})
        _M_SYNC.inc(("not_configured",))
        return False
    if _sync_paused:
        # A previous conflict parked work on a branch; do nothing until a
        # human resolves it and clears the pause. Avoids unbounded conflict branches.
        log.warning("Git sync is paused after an earlier conflict; skipping.",
                    extra={"event": "git_sync", "result": "paused"})
        _M_SYNC.inc(("paused",))
        return False
    try:
        # Commit BEFORE pulling so local work is a real commit, not just a
        # dirty tree. This makes the rebase well-defined and recoverable.
        repo.git.add(all=True)
        if repo.is_dirty() or repo.untracked_files:
            safe_msg = message.replace('"', '').replace("'", "")
            repo.index.commit(safe_msg)

        # Local-only mode: a repo initialised without a remote (no GIT_REPO_URL).
        # Committing above already captured the work; there is nothing to pull or
        # push, and attempting to would spuriously park the tree on a conflict
        # branch. Report success after the commit.
        if not repo.remotes:
            _M_SYNC.inc(("local_only",))
            log.info("Git sync committed locally (no remote)",
                     extra={"event": "git_sync", "result": "local_only"})
            return True

        try:
            # No forced "ours" strategy.
            repo.git.pull(rebase=True, autostash=True)
        except exc.GitCommandError:
            if not _rebase_in_progress():
                # The pull failed without ever starting a rebase, so there is no
                # divergence: the remote is empty (a brand-new private repo has no
                # branch to merge with) or unreachable (outage, rotated deploy key).
                # Neither is resolvable by a human merge, and latching the pause on
                # them would disable the ADR-4 mirror until someone noticed and
                # called resume_sync. Try the push instead — for an empty remote it
                # publishes the first branch; for an outage it fails and we retry
                # on the next sync.
                log.warning("git pull failed without starting a rebase (empty or unreachable "
                            "remote); attempting push and retrying next cycle.")
                return _push(first_push=True)
            log.warning("git pull --rebase hit a conflict; parking local work and aborting sync.")
            try:
                repo.git.rebase("--abort")
            except Exception:
                pass
            # Park the current (committed) local HEAD on a conflict branch and
            # STOP. Pushing the diverged branch would be rejected (non-fast-forward) and
            # the sync would wedge on every subsequent cycle. The branch is the signal.
            try:
                branch_name = f"conflict-{secrets.token_hex(4)}"
                repo.git.branch(branch_name)
                _sync_paused = True  # latch: no further syncs until manually resolved
                log.warning(
                    "Local changes preserved on branch %s — manual merge required. "
                    "Auto-sync is paused until the divergence is resolved.", branch_name)
            except Exception:
                log.exception("Could not create conflict branch.")
            _M_SYNC.inc(("conflict",))
            log.warning("Git sync parked on conflict branch", extra={"event": "git_sync", "result": "conflict"})
            return False

        # Rebase succeeded (or was a no-op). Push if we are ahead of the remote.
        return _push()
    except Exception:
        log.exception("Git Sync Error occurred.")
    _M_SYNC.inc(("error",))
    return False


def resume_sync() -> None:
    """Clear the conflict pause once the divergence has been resolved manually."""
    global _sync_paused
    _sync_paused = False
    log.info("Git auto-sync resumed.", extra={"event": "git_sync", "result": "resumed"})


def is_sync_paused() -> bool:
    """Expose the pause latch so an operator can tell whether a conflict parked sync."""
    return _sync_paused


@register(
    "sync_status", "Report whether git auto-sync is healthy or paused after a conflict.",
    {"type": "object", "properties": {}},
)
async def sync_status_tool(arguments: dict) -> list:
    if repo is None:
        return text("Git sync is not configured (no repo). Vault changes are local only.")
    if _sync_paused:
        return text(
            "Git auto-sync is PAUSED after a merge conflict. Local work was parked on a "
            "conflict-* branch. Resolve the divergence in the repo, then call resume_sync.")
    return text("Git auto-sync is active.")


@register(
    "resume_sync",
    "Clear the git auto-sync pause after you have manually resolved a conflict branch.",
    {"type": "object", "properties": {}},
)
async def resume_sync_tool(arguments: dict) -> list:
    if not _sync_paused:
        return text("Git auto-sync is not paused; nothing to resume.")
    resume_sync()
    return text("Git auto-sync resumed. The next write (or the 15-minute cron) will sync.")


async def git_worker():
    """Serialize all git operations through a single worker to avoid races."""
    assert git_queue is not None
    while True:
        message = await git_queue.get()
        try:
            await asyncio.to_thread(sync_git, message)
        except Exception:
            log.exception("git_worker failed for message: %s", message)
        finally:
            git_queue.task_done()
            _M_QUEUE.set(git_queue.qsize())


def start_worker():
    """Create the queue and launch the worker task (call from server startup)."""
    global git_queue
    git_queue = asyncio.Queue()
    return asyncio.create_task(git_worker())


async def enqueue_sync(message: str):
    """Queue a git sync without blocking the caller."""
    if git_queue is not None:
        await git_queue.put(message)
        _M_QUEUE.set(git_queue.qsize())
    else:
        # Fallback (e.g. tests without the worker running): run off-thread, best-effort.
        await asyncio.to_thread(sync_git, message)

