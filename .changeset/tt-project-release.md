---
"tt-project": minor
---

Add the optional `tt-project` plugin: long-running, self-driving projects for Claude Code, Codex and
Cursor that run on your own machine or an always-on box, with no hosted service. Give a project a
brief; its coordinator plans the work, hands it to headless workers and stays inside a budget. It
keeps going when the chat closes, and any later chat reconnects to it by name.

**Install**

- One-line installer (`install.sh`): checks for `git` and Python 3.9+, registers the
  `tenstorrent-skills` marketplace for each agent CLI it finds, installs the `ttp` command and
  verifies it. Rerun it to update; `TT_PROJECT_REF` picks a release tag, branch or commit.
- `ttp setup`, `ttp doctor`, `ttp upgrade` and `ttp uninstall` (`--dry-run` shows each step).
  The daemon runs as a user service, with a crontab fallback where there is none.

**Runtime**

- One deterministic daemon per project: schedules, model-free watchers (pull requests, CI, logs,
  shell commands), budget gates and message routing. It starts a model only for a run.
- A short, tool-less coordinator turn that decides; workers that each do one task in their own git
  worktree, with a dollar budget, a wall clock and a stall guard, and hand off a structured result.
- Spend control: plan-window headroom or dollar caps, a runaway guard, model tiers and effort per
  task, optional Jev screening, context splitting and trimmed command output (`ttp clip`,
  `ttp checks`).
- Survives sleeps, reboots and lost sessions: interrupted runs resume or retry without spending an
  attempt, and hosts that keep crashing are flagged.
- Shared resources: `ttp lock` and a device queue for devices and machines, a per-machine change
  ledger with undo, fleet and machine pauses, and `ttp killscan` / `ttp svcscan` checks for
  scripts that would kill or change other people's processes and services.
- Delivery: a push queue that rebases, runs the project's checks, batches version bumps and never
  force-pushes; a review step before any push; draft pull requests only.
- Process sandbox: `ttp checks` and push checks run each local check in its own PID namespace
  (`unshare` or `bwrap`), so a test cannot see or signal other processes; `ttp sandbox -- <cmd>`
  does the same for any command. Where the host allows no user namespaces, checks run as before
  and say why; `ttp doctor` names the setting.
- Optional failover: standby state snapshots and a project lease, so only one host coordinates.
- Plain-file memory and charter; entries with an end date or condition retire themselves.

**Web app**

- A local web app per project, protected by a per-project token: status, running work, blockers,
  spend, alerts and a projects overview. Browser notifications, and `ttp web --tunnel` for
  access from another machine.

**Chat relay**

- Start, connect to and talk to a project from any chat; your words reach the coordinator verbatim
  and its replies and alerts come back to that chat. `ttp say --all` broadcasts to every project.
- `ttp status` and `ttp overview` report work, blockers, spend and why a project is idle.
- Alerts are limited to decisions, reviews, merges, funds and outages; an optional desktop
  notifier covers all projects on a workstation.

**Skills**

- `tt-project`: start, connect to and talk with a project.
- `tt-project-harness`: improve a project's own prompts, schedules, watchers and budgets from
  measured friction, and take template updates.
- `setup-box`: get a Tenstorrent device box (bare metal, reservation or a Slurm cluster) and work on
  it over ssh in a checkout on the box.
