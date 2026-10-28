---
name: setup-box
description: "Get a Tenstorrent device box and work on it over ssh: pick a route (bare metal, IRD reservation, Slurm or exabox), keep a checkout or git worktree on the box, edit, build and run there, and report host, path, commit and job ids back. Use when a tt-project task needs a device box, or the user asks to set one up."
---

# tt-project: setup-box

## Purpose

- The project box only coordinates. Device work happens on the device box, over ssh.
- This skill gets a box, sets up a checkout there, and says what a box task reports back.
- Site values (aliases, paths, partitions, pools) live in the local site file, never in a repo:
  `site.md`.

## When to Invoke

- A task needs a device box, or a box it used is gone.
- The user asks to set up, reserve or allocate a box.
- A fresh box needs setting up (broker, host checks, workspace, env, agent CLIs, access): ask the
  user first, then set it up: `new-box.md`. It is set up in the user's own account, created when
  missing, not in a shared admin login: `account.md`.
- A project still syncs or mirrors files to a box: `migrate-off-sync.md`.
- Claude Code or Codex is missing, logged out or absent from the user's editor (Cursor, VS Code)
  on a box: `box.py agents check <box-alias>` names each gap and the command the skill runs to fix
  it: `agents.md`. A login link lapses: start one only inside the user's usual hours
  (`ttp hours --check`, see `agents.md`).
- Setting up this machine or a box also covers the user's claude.ai connectors (Atlassian, Google):
  `box.py connectors check [<box-alias> ...] --verify` checks the same account as this machine,
  each connector's state and one real read-only call per family: `connectors.md`.
- In a headless worker run (`claude -p`), do not assume claude.ai connectors are present, even when
  `claude mcp list` shows them connected: check that their tools are listed before relying on one,
  else fall back to other configured MCP tools.

## Route picker

Read the box's entry in `~/.tt-project/boxes.json` (by its `ttp machines` alias) and check it
with `<plugin-root>/skills/setup-box/scripts/box.py site check`. Its `route` picks the page.

| You have | Route | Page |
|---|---|---|
| A machine you can ssh to and own for now | `bare` | `bare-metal.md` |
| A reservation system with a management host | `ird` | `ird.md` |
| A Slurm cluster (1-chip, 4-chip or whole-node jobs) | `slurm` | `slurm.md` |
| A Slurm cluster with whole-node exclusive partitions | `slurm` | `exabox.md` |
| No entry yet | ask the user for the route and aliases, write the entry | `site.md` |
| A fresh box to set up | questionnaire, then each answer as an action and its check | `new-box.md` |

All routes share `ssh-workflow.md` (aliases, jump host, detached jobs) and `agents.md` (agent CLIs
and build/run recipes on the box).

## Etiquette on shared machines

- Get a box through its scheduler or reservation system; never ssh into a node you hold no
  allocation or reservation for.
- Never take a node or box that is allocated, drained, reserved or down, and never cancel, join or
  share another user's allocation.
- Reserve or allocate only for the run, release right after it, and re-reserve when needed. Never
  auto-extend a reservation, and hold nothing idle.
- A project's charter can narrow these rules (which machines, how long); it never widens them.

## Work on the box

- The device task ssh-es to the box and edits, builds and runs in its own checkout or
  `git worktree` there (`ssh-workflow.md` names and cleans them).
- It commits and pushes from the box, or reports a diff or commit id.
- Never sync, mirror or copy the project box's files to the box (no rsync, no sshfs, no scp of
  sources). The repository is the only channel. A job input or result that must move goes through
  `scripts/box.py copy` (rsync, else tar over ssh): `device-runs.md`.
- Long work runs detached on the box with a log and an rc file (`ssh-workflow.md`); the task hands
  off `waiting` with a `retry_when` that reads the rc file.
- Device jobs go through the box's queue or broker and carry a build stamp (commit, dirty flag,
  built artifact): `device-runs.md`.
- Classifying a chip drop: an empty holder or killed list from the broker when the loss is
  detected does not prove nothing was driving the chip, since short jobs can exit between health
  probes. Decide whether the loss was provoked from the system journal and the run output; use the
  holder set only as corroboration.

## Storage

- `workdir_root` (site file): shared storage for checkouts and build trees that must outlive a job.
- `scratch_root` (site file): node-local scratch, fast but wiped when a job ends on some clusters.
  Copy results you keep back to `workdir_root` before the job ends.
- Never assume a path: take both from the site file, and check free space with `df <path>`.
- Shared network storage mounted by hand is gone after a reboot: make it persistent and verify it
  now and after the next reboot: `storage.md`.

## Hand-off from a box task

A box task's result.json says:

| Field | What |
|---|---|
| host | the `ttp machines` alias (never a raw hostname) |
| path | the checkout or worktree path on the box |
| commit | the commit it built or ran, and whether it was pushed (branch) or only reported (diff) |
| jobs | job or reservation ids it started, with start and end, and each device job's build stamp |
| still running | what is still running or held on the box, or "nothing" after checking |
