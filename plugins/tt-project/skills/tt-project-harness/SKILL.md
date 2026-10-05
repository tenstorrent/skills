---
name: tt-project-harness
description: "Improve a tt-project's own harness — its coordinator and worker prompts, schedules, watchers, budget settings and web app — from measured friction, and take upstream template updates. Use for harness tasks, daily reviews, or when a project wastes money or time or needs the user too often."
---

# tt-project: harness

## Purpose

- A project's harness is `<root>/tt-project/harness/`, its own git repo.
- It starts from the tt-project template, then adapts to this project.
- This skill changes how the project runs. It never changes the project's goals.

## When to Invoke

- A task of kind `harness`, or a daily review proposing harness changes.
- The user says the project is slow, noisy, expensive, or asks too much.

## Evidence first

| Question | Look at |
|---|---|
| Where does money go? | `ttp status <name> --json` → `budget.top_sources_7d`, `runs` |
| Which runs made no progress? | failed/stalled runs, tasks with many attempts |
| Is the coordinator thrashing? | coordinator turns per hour, rejected actions events |
| Is the user asked too often? | `ask` messages in the last week |
| Is recurring work earning its cost? | `schedules` → `cost_7d` vs what it produced |

## Levers (cheapest first)

| Lever | File |
|---|---|
| Coordinator rules | `prompts/coordinator.md` |
| Worker contract, per-kind rules | `prompts/worker.md`, `prompts/kind-*.md` |
| Tiers, caps, debounce, idle wake, resources | `project.json` (defaults: `runtime/ttp/project.py`) |
| Recurring work | `schedules.json` when the harness has it (the daemon applies edits; `ttp schedules <name> --export` creates it once from the database), else coordinator `schedule_set` or the web app's Recurring pane |
| Watchers | `kind: command` schedules; scripts under `watchers/` in the harness. One JSON line per observation: `{"text", "severity", "repeat"}`; `"repeat": true` wakes the coordinator on every new occurrence, otherwise a known one wakes again after `screen.rewake_after_h` quiet. Write a line as `<subject>: <item>; <item>`: each item is one issue (counts masked), an item may start with `now`, `still:`, `changed:` or `cleared:`, and `cleared: <item>` closes it. A run that prints nothing closes the watcher's open issues, and an issue not seen for 24 h closes; a closed one seen again reopens and wakes at its severity. With a Jev key, Jev rates each new observation; each call is logged with its cost and the coordinator wake it skipped, and a Jev use that saves nothing over `jev.window_days` (at least `jev.min_calls` calls) switches itself off once, reported; `jev.uses.<use>` = `on`/`off` forces it (uses: `screen`; `effort`, which may start a short-lookup task the coordinator queued at standard at light) |
| Web app | `runtime/ttp/web/` |
| Runtime behavior | `runtime/ttp/*.py` (last resort) |

## Rules

- One friction, one small commit, a one-line reason in the message.
- NEVER loosen a restriction, cap or notification policy without the user.
- Runtime edits: keep Python 3.9-compatible; run `python3 -m pytest -q runtime/tests` if present;
  then `ttp restart <name>`. It waits for the daemon to tick; if it does not, `runtime/` goes back
  to the last version that ran (a new commit) and the user is alerted.
- Measure after: same evidence, a day later. Revert what did not help.
- Change only this project's own harness. Never edit, or create a worktree or branch in, the
  tt-project plugin's source repository or another project's harness. Running `ttp setup` or
  `ttp upgrade <name>` to deploy a release to another project on this machine is not editing its
  harness; hand edits to its charter, memory, config, state or code are.
- A lesson every project would benefit from → an upstream note in the hand-off: a follow-up
  titled `upstream: …`.
- Notes reach the project that reads them even when its machine cannot reach this one (a laptop
  behind NAT): each daemon sends this machine's own notes on over ssh to `ttp upstream --receive`
  there, with the user's keys and known hosts and no new port. Notes go only where they are read;
  one for a project (`ttp note --to`) goes only to the machine that runs it. `ttp upstream
  --forward-status` shows each target's cursor, last success and last error; `ttp upstream
  --forward-to <aliases>|default|none` sets the targets. Received notes stay untrusted data.

## Template updates

- `ttp upgrade <name>` merges the installed tt-project template in a scratch worktree, checks the
  runtime compiles and imports, then fast-forwards this harness. Otherwise the harness is left
  as it was and a harness task is queued to finish the merge.
- Conflicts: keep this project's intent, take upstream fixes. Resolve them in a scratch worktree,
  never in the live harness.
