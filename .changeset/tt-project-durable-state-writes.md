---
"tt-project": patch
---

`tt-project`: State survives a power cut. Memory entries, the memory index, the charter, project config,
secrets, run specs (`run.json`), exit records, heartbeats, locks, waits and service files are now written
to a temporary file, synced and renamed into place, with the directory synced after; appends (memory index,
charter updates, steer notes, progress notes) are flushed and synced. A test lists every remaining direct
write in the runtime with the reason it need not survive a power cut. Harness commits and workers' git
commands sync object files (`core.fsync=committed` on git 2.36 and later, `core.fsyncObjectFiles` before),
passed per command and through `GIT_CONFIG_COUNT` after any keys already set; no repository's config is
changed. On the first daemon start of a new boot, a check (well under a second on a typical harness) runs
a bounded `git fsck --connectivity-only` on the harness only, puts back empty or cut-short memory entries,
memory index, charter and config from the last commit or last good copy, moves a damaged never-committed
entry aside, and reports a task worktree whose `git status` fails. What it cannot repair raises one alert,
which clears once a later check passes. An empty or truncated `run.json` now fails that run cleanly
instead of crashing its supervisor.
The check keeps a hand edit that drops the charter's last section (only a cut mid-line counts as a
cut write) and saves whatever a restore replaces under `state/damaged/`. An empty `web.token` gets a
new token instead of accepting an empty one, and the relay outbox's rewrite and appends sync their
directory too.
