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
| Recurring work | `schedules.json` when the harness has it (the daemon applies edits; `ttp schedules <name> --export --yes` moves them there once and commits; to only read one, `ttp schedules <name> show <schedule>`), else coordinator `schedule_set` or the web app's Recurring pane |
| Watchers | `kind: command` schedules; scripts under `watchers/` in the harness. One JSON line per observation: `{"text", "severity", "repeat", "whole"}`; `"repeat": true` wakes the coordinator on every new occurrence, otherwise a known one wakes again after `screen.rewake_after_h` quiet. Write a line as `<subject>: <item>; <item>`: each item is one issue (counts masked), an item may start with `now`, `still:`, `changed:` or `cleared:`, and `cleared: <item>` closes it. A JSON line with `"whole": true` is one observation instead: one issue for all the text after its subject, never split at `; ` (a long title is truncated); a leading `cleared:` closes it. An optional `"key"` (a short stable string) is the issue's identity instead of its text, so a changed text under the same key updates that issue and opens no new one. A multi-line observation without a key is keyed by its first line: make it a stable heading and list the changing items below it. An info line (`"severity": "info"`) takes the same path as any other: its `cleared:` items close issues and it replaces older receipts of the same subject. Known gap: info lines are not suppressed, so an item first seen at info is still recorded as a quiet issue (it wakes once a line rates it higher). A run that prints nothing closes the watcher's open issues, and an issue not seen for 24 h (or 3 periods of a slower watcher) closes; a closed one seen again reopens and wakes at its severity. An event that must stay until its owner acknowledges it (a receipt): print it every run, with its identity as a prefixed token (`evidence_<hash>`: bare hashes and numbers are masked), until the owner acknowledges it, and set `rewake_after_h` null so an unchanged one never wakes again; a changed token wakes. A receipt source that may miss runs or report several subjects also sets `issue_lifecycle` `explicit_clear`: its items then never close by time (downtime included); one closes when the owner acknowledges it (`cleared: <item>`, or a run that prints nothing) or when a run reports a different item for the same subject (the changed outcome wakes; a later return to the old one wakes too). A watcher's own failures (timeout, failed exit code with no output, or a line with `"error": true`) are errors under `watcher-error:<name>`, never receipts: they close after the usual quiet time or at the next successful run that no longer reports them, so the same failure later wakes again. With a Jev key, Jev rates each new observation; each call is logged with its cost and the coordinator wake it skipped, and a Jev use that saves nothing over `jev.window_days` (at least `jev.min_calls` calls) switches itself off once, reported, as does one whose last `jev.idle_calls` (20) calls changed none of the rules' decisions; `jev.uses.<use>` = `on`/`off` forces it (uses: `screen`; `effort`, which may start a short-lookup task the coordinator queued at standard at light) |
| Self-healing checks | A `kind: command` schedule with a `heal` block in `payload` (`schedule_set` field `heal`): `check` (shell: exit 0 healthy, 1 unhealthy, 75 or 255 unknown, which never fixes) or a `preset` (`systemd`: `unit`, `user`, `host`, unit active and NRestarts not rising, default fix a restart; `http`: `url`, `expect` status codes; `broker`: `status_command` printing JSON with no `hold_field` set and every `auto_fields` flag on), `fix` (optional), `resource` (the fix runs under `ttp lock`; busy or paused defers it uncounted, a lock busy (not paused) past `window_h` escalates, and a fix that itself exits 75 counts), `grace_s` (300), `settle_s` (60), `max_fixes` (3) per `window_h` (1), `timeout_s` (120, capped at 240 so a step never outlasts the daemon's watchdog; a timeout kills the whole process group). The daemon runs it with no model: unhealthy past grace runs the fix and rechecks after settle; a fix that worked is one low feed line and a digest record, with no coordinator wake. A failed fix, a used-up cap or no fix queues one priority-1 self-fix task per check (label `heal:<name>`) carrying the check and fix output; only when that task fails, blocks or ends with the check still failing does a high outage alert `heal:<name>` go up, and it clears itself once the check passes. `escalate` (`user` by default, or the project's `heal.escalate` config) picks who gets that step: `coordinator` sends no user alert and instead queues one high observation per unhealthy episode for the coordinator (fingerprint `heal:<name>:<since>`, never repeated within the episode). `known_fault` (a reason) keeps a known single fault quiet, but with `outage: true` (a whole box or resource that serves nothing) it never stays quiet past `grace_s` or 60 min, whichever is first, also while its check stays unknown (an unreachable box): that escalates without running the fix and clears on the next healthy result; mutes never fold it. A check that finds a problem while the box is up (a non-outage finding) exits non-zero and prints a line `ttp-heal: not-outage`: that run counts as plain unhealthy as if `outage` were off (its own `grace_s`, the fix, `known_fault`, the ordinary self-fix task, no outage wording and no machine ledger `down` report); a check that never prints the line keeps the outage behaviour. `ttp heal list|test <check>` shows the checks and runs one dry (no fix); `ttp status` and the web app show `health: N ok, M fixed today, K failing`. |
| Box-clean gates | `tt-box-clean-probe` (in the harness's `bin/`, on every probe's PATH) answers "has this box been N hours without a device incident?" with no model, read-only: host uptime plus the tt-device-mcp broker's recent jobs (boots, broker resets, power-cycles, reboots, PCI rescans, health gates, hold starts or a device held degraded, failed broker jobs, tenant jobs the broker killed (or left `[MCP killed ...]` by a chip drop), found hung or that ran a reset), each dated by when it finished (a running job or a hold still on counts as now), in the host's local time as the broker writes it. Use it as a `start_when`, `retry_when` or resource-pause `end_when` instead of a daily light run that only checks: `tt-box-clean-probe --window-h 24` on the box itself, `ssh <box> python3 - --window-h 24 < "$TTP_PROJECT/harness/bin/tt-box-clean-probe"` for another. Exit 0 clean, 1 not yet (prints the newest incident and the earliest clean time), 255 cannot tell. With no broker installed it judges on uptime alone (`--require-broker`: 255 instead); `--broker-python` (env `TT_BROKER_PYTHON`), `--port`, `--limit`, `--jobs-file` (JSON instead of the broker). |
| Model-free wait endings | A waiting hand-off's `on_pass` (the final hand-off, recorded when the run's own `ttp checks --detach` pass) or `on_pass_cmd` (a command the daemon runs from the project root once `retry_when` passes, killed after `waiting.on_pass_timeout_s`, 600 s; exit 0 with a `done` hand-off written to `$TTP_RESULT` ends the task with no model run, anything else wakes it with the command's output tail). Runs ended this way show `run_status` `model-free`. |
| Shared watchers | A watcher script that workers also run (at start or hand-off) takes a non-blocking lock (`flock -n` on a file under the harness state, or `ttp lock` with a short wait) and exits quietly if busy, re-checks the condition before it acts, and dedupes by condition key. |
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

## Rebooting the harness's own host

A reboot ends every run in flight, and nothing lifts a plain pause afterwards: schedules and probes
do not run while the project is paused. A task that must reboot the host its harness runs on:

1. `ttp drain <name> --wait 240`: no new runs or pushes start; it exits 0 once none is in flight
   (the calling run does not count) and 1 at the timeout, with the project left paused. Running
   workers are never killed. Keep the wait inside one tool call (a few minutes). On exit 1 run it
   again in further tool calls of the same run, at most 3 times. Still not drained: `ttp resume
   <name>` and hand off `waiting` with a time-based `retry_when` (`test "$(date +%s)" -ge <epoch>`).
   Never leave a paused project with a waiting task: probes do not run while paused, so it never wakes.
2. `ttp pause <name> --until-reboot`: the daemon lifts this pause on its first tick after the host
   booted again and says so in the feed. A plain `ttp pause` or `ttp resume` replaces it.
3. Write the hand-off (`waiting`, with a `retry_when` that holds once the host is back, and no
   `survives_reboot`: a reboot then wakes the task at once), and start the reboot a little later
   (for example `shutdown -r +2`), so the run ends cleanly first.

The boot is told by the kernel's boot id (on macOS, kern.bootsessionuuid, else kern.boottime); where none can be read,
`--until-reboot` is refused.

## Template updates

- `ttp upgrade <name>` merges the installed tt-project template in a scratch worktree, checks the
  runtime compiles and imports, then fast-forwards this harness. It settles conflicts that need no
  judgment itself (both sides only added at one spot, or upstream already ships the change).
  Otherwise the harness is left as it was and a harness task is queued to finish the merge, at
  most one a day. It then exits 75 (deferred, not failed) while that task or the daemon's retry under
  `upgrade.auto` takes it on (the daemon retries only a newer version); when nothing does, it exits 1.
  The same conflict deferred past 48 h, or after two harness tasks that did not land it, is
  reported once as a normal observation.
- Conflicts: keep this project's intent, take upstream fixes. Resolve them in a scratch worktree,
  never in the live harness, and apply the result with `ttp upgrade <name> --apply <commit>`.
  Where the service manager is out of reach (a sandboxed worker), it asks the running daemon to
  restart itself (state/restart.request) and rolls nothing back while the old daemon still ticks.
  Exit 75: applied, restart deferred to the daemon; exit 1: the restart failed or was rolled back.
- One merge at a time: `ttp upgrade` holds the `harness-upgrade` lock for the whole merge, refuses
  while another upgrade runs, and stops if `main` changed the template's files meanwhile. An open
  upgrade task holds the live harness until it ends: a newer release moves `upstream` and retargets
  that task (exit 75) instead of queuing another. The harness repo refuses any other merge of
  `upstream` into `main`.
