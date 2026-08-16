---
name: sync-health
description: How to check git auto-sync health and recover it after a merge conflict parked local work.
when_to_use: "When a write seems not to reach the git mirror, before ending a session that made important changes, or when you suspect sync is stuck after a conflict."
version: 1
---

# Sync Health & Recovery

Every vault write is mirrored to a private git repo by a background worker. The
markdown files are the source of truth; if sync stalls, your work is NOT lost — it is
just not yet pushed. On a merge conflict the server PAUSES auto-sync and parks local
work on a `conflict-*` branch rather than discarding or force-pushing anything.

## Checking
Call `sync_status`. It returns one of:
- **"active"** — healthy, nothing to do.
- **"not configured (no repo)"** — no git remote is set up; vault changes stay local
  only. This is expected in local-only deployments, not an error.
- **"PAUSED after a merge conflict"** — auto-sync is halted; local work sits on a
  `conflict-*` branch and needs manual resolution.

## Recovering from a pause
1. The conflict must be resolved **in the git repo itself** (merge/rebase the
   `conflict-*` branch, or reconcile with the remote) — this happens outside the MCP
   tools, by whoever operates the server.
2. Once the divergence is resolved, call `resume_sync` to clear the pause.
3. The next write (or the 15-minute cron fallback) will sync again.

Do not spam writes hoping sync recovers on its own — while paused, nothing is pushed.
Surface the paused state to the user plainly instead.
