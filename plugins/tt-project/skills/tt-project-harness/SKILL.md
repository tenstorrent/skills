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

A row that links to a file under `references/` keeps its details there, one sentence per line:
read the one for the lever you change.

| Lever | File |
|---|---|
| Coordinator rules | `prompts/coordinator.md` |
| Coordinator session | Always on, no setting: one persistent session per project (`prompts/coordinator-session.md`). A failed resume or a rollover starts the next session seeded with the hand-off (`state/coord-session/`); it never falls back to a stateless turn. `coordinator.session` in `project.json` only sizes the rollover and tools |
| Worker contract, per-kind rules | `prompts/worker.md`, `prompts/kind-*.md` |
| A hand-off rule that must hold every time | `worker.handoff_checks` in `project.json` (see docs/guide.md): a command run on each hand-off before it is accepted or its reply posts; exit 1 sends the run back with the output. Use it instead of a `worker.md` rule when a script can test the rule; a prompt rule gets missed, and a schedule backstop catches the miss only after the reply went out |
| Tiers, caps, debounce, idle wake, resources | `project.json` (defaults: `runtime/ttp/project.py`) |
| Task pin verification | `runner.pin_verify` (`lenient` by default, or `strict`): whether an explicitly model/effort-pinned task runs when its provider cannot verify the pin. See [references/task-pin-verification.md](references/task-pin-verification.md) |
| Providers a project may use | `providers_allowed` (unset: every configured provider): tier routing, failover and task pins never leave it. See [references/providers-allowed.md](references/providers-allowed.md) |
| Recurring work | `schedules.json` when the harness has it (the daemon applies edits; `ttp schedules <name> --export --yes` moves them there once and commits; to only read one, `ttp schedules <name> show <schedule>`; to run a command schedule now, also from a sandbox, `ttp schedules <name> run <schedule>`), else coordinator `schedule_set` or the web app's Recurring pane. An llm schedule's `precheck` (shell, from the project root, after its budget, open-run, debounce and idle gates; `precheck_timeout_s` 120, capped at 240) skips a trigger with no model run when it exits 0 (status `skipped: precheck: <first output line>`); any other exit, a timeout or an error runs it. Use one where a script can tell the window is empty. Host-reaching commands: see [references/host-reaching-commands.md](references/host-reaching-commands.md). Budgets (`budget_usd` daily, `budget_usd_run` per run), the skip history and run-now: see [references/schedule-budgets.md](references/schedule-budgets.md) |
| Memory tidy-up | `ttp memory <name> --forget <entry>...`, `--supersedes`, `--search`; daily model-free archiving. See [references/memory-tidy-up.md](references/memory-tidy-up.md) |
| Correction class fixes | A Course check `recurring mistake` line whose class's fix task was cancelled while another task or commit fixed the class: `ttp task <name> superseded <cancelled id> '#<done task>'` (or a commit sha in the harness, the project root or `--repo`) links the later fix; the line stops while that task stays done. Open, failed or cancelled fix tasks, another project's task and unknown commits are refused. A fix followed only by llm schedules keeps a `report-only follow-up` line until a model-free gate checks the action followed. See [references/correction-class-fixes.md](references/correction-class-fixes.md) |
| Retired-rule echoes | After a charter `quote`/`replaces`, a memory `supersedes` or a user Correction, the digest lists leftover copies of the old text (memory, charter, schedules, open tasks, config): only a rule the user withdrew is a fix order, the rest advisory. `ttp echoes --json` lists every retirement and match, read-only; `ttp echoes --ack <retirement> <path>:<line>` acknowledges one false positive until its line changes. See [references/retired-rule-echoes.md](references/retired-rule-echoes.md) |
| Data kept out of git | A charter `Forbidden data` section, `memory.forbidden_patterns` and `memory.forbidden_patterns_file`: memory and charter writes refuse matching text. See [references/forbidden-data.md](references/forbidden-data.md) |
| Watchers | `kind: command` schedules; scripts under `watchers/` in the harness. Output lines (`key`, `episode` and stepped alerts, `whole`, `pin_severity`, `cleared:`), exit codes and errors, paging another project, Jev screening, snapshotting launchers, kernel logs across boots. See [references/watchers.md](references/watchers.md) |
| Driver progress reports | Every detached or looping driver (filler, recheck loop, guard) runs `ttp progress report result\|refusal\|stall\|failure --key <key> "<reason>"` for each counted result, refusal and failure: one locked, rotated JSON line in state/progress.jsonl, daemon up or not. Repeats with no result raise one keyed observation; `ttp status` and the web app show the driver as stalled. A `ttp detach` job that writes a file per finished unit can declare them instead: `--results '<glob>'` (relative to its working dir). A job that calls a paid API takes a model-free spend cap: `--spend-cmd "<cmd>" --spend-cap <usd>`. See [references/driver-progress.md](references/driver-progress.md) |
| Long-running scripts | A worker's watcher thread (reentrant lock, bounded acquires, stall guard) and a script in a shared non-git directory (private snapshot at launch, sha256, re-exec marker, edits via temp file plus `mv`). See [references/long-running-scripts.md](references/long-running-scripts.md) |
| Self-healing checks | A `kind: command` schedule with a `heal` block, run by the daemon with no model: `check` or `preset`, `fix` (`fix_detached` for a long one), `suppress` (deliberately down: no fix), `resource`, escalation, outages, known faults and pauses; `ttp heal list\|test <check>`. See [references/self-healing.md](references/self-healing.md) |
| Box-clean gates | `tt-box-clean-probe` (in the harness's `bin/`): a model-free, read-only probe for "has this box been N hours without a device incident?". See [references/box-clean-gates.md](references/box-clean-gates.md) |
| Readiness probes | A probe that waits for a device or box checks it live, never a bare marker file; a broker hold's end with `ttp broker-released`; exit 255 is ssh's own only; a probe prints one short line naming what it waits on, which the wait note shows; a user hold is `test -e state/user-<x> || exit 1`. See [references/readiness-probes.md](references/readiness-probes.md) |
| Service changes in drivers | `ttp svcscan <script>` flags a driver's service unit changes (`systemctl stop`, `mask`, `restart`, ...; `service <unit> stop`) before it runs on a shared machine; `ttp svcscan --shim <dir>` stand-ins only log them. See [references/service-changes.md](references/service-changes.md) |
| Time to recover | `ttp ttr`: model-free time to recover per machine, from incidents the daemon records; incidents during a project's own `ttp machines change add --test` entries are listed apart. See [references/time-to-recover.md](references/time-to-recover.md) |
| Checks on another host | `delivery.checks_host` (an ssh alias) runs every check there; a check's own `"host"` overrides it. An unreachable host makes the check wait, never pass or fail. Heavy suites belong there when the project has an allowed host: here they run at nice 19, within `checks.test_project_workers` and not while the host is overloaded (`checks.load_guard`). See [references/push-checks.md](references/push-checks.md) |
| Checks fast path | `delivery.checks_fast_globs` (opt-in, off by default): a diff whose paths all match runs only the whitespace and CRLF checks. See [references/push-checks.md](references/push-checks.md) |
| Checks pre-stage | `ttp checks --detach` first runs a short pre-stage in the foreground; its failure starts nothing in the background. `delivery.check_prestage` names the command; unset, it is derived from the project's frequent fast check failures; `''` or `false` turns it off. See [references/push-checks.md](references/push-checks.md) |
| Push check priority | `checks.host_max` slots shared host-wide; pushes to the push branch get a priority slot. See [references/push-checks.md](references/push-checks.md) |
| Push check outputs | `delivery.push_checks` get a writable absolute `TTP_RUN_DIR`, outside the checked worktree. See [references/push-checks.md](references/push-checks.md) |
| Own pushes | `delivery.own_push`: `on`, `existing_only` (also unset) or `off`. See [references/own-pushes.md](references/own-pushes.md) |
| Release-channel tags | `delivery.channel_tags` lists tags a release cut moves forward; `ttp push --move-tag <tag> <commit>` moves one under a fresh lease, also under `delivery.never_force`. See [references/release-channel-tags.md](references/release-channel-tags.md) |
| Landed waits without a push branch | `delivery.landed_probe`: a read-only command, `{id}` the task id (exit 0 landed, 1 not yet, else broken), that answers `landed:#<id>` when `delivery.push_branch` is unset. Unset too: automatic chains use `depends_on`; a coordinator `start_when landed:#<id>` becomes `depends_on <id>` (other `&&` terms kept), `ttp task add`/`set-when` refuse it, and stored or worker-written waits pass once the task is done |
| Hand-off lint | Model-free, on every code hand-off: flags a follow-up to push an own branch that is not on the remote. See [references/own-pushes.md](references/own-pushes.md) |
| Requested cells | A spec line `cells: a, b; c` names the measurements or tests a task must cover, as short names (one per chip id or test), never a description: a descriptive line is refused at task_add/task_update and, stored from before, ignored; its done hand-off lists the ones finished with a valid result in `cells`: a list of names, or a dict name -> row; a dict value that is empty, `valid: false` or a string starting `invalid` is not finished (0 is a measurement). Model-free: a done missing some reads `partial: N of M cells` and gets a `continue:` follow-up for the rest; one without `cells` reads `cells unreported: M requested`. The status stays done. Cells no hop could produce (`cells_unrunnable`) get none: see [references/continue-hops.md](references/continue-hops.md); a wrong mark is corrected with `ttp task <project> rejudge <id>` (same file) |
| Model-free wait endings | A waiting hand-off's `on_pass` or `on_pass_cmd` ends the task with no model run; `ttp task set-when`, `ttp ack-wait`, a measured `eta` with `eta_basis` for a wait on outside work, and where a detached `ttp checks` keeps its output. See [references/model-free-wait-endings.md](references/model-free-wait-endings.md) |
| SSH preflight | `ttp ssh-preflight <host>` or `@<resource>`: a strict ssh check whose exit codes tell gone, wrong key, no auth and a sudo ProxyCommand that no_new_privs breaks apart. See [references/ssh-preflight.md](references/ssh-preflight.md) |
| Stock goal checks | `ttp goal suite_green\|artifact_fresh\|service_up`: model-free "is this goal met?" for a probe, `on_pass_cmd`, heal check or watcher. See [references/goal-checks.md](references/goal-checks.md) |
| The ask test | Ask the user only on harm to people or speaking as the user; inputs, space and routes are decided (accessible copy first, then a location with room). See the Asks section of `docs/guide.md` |
| Project ask-gate rules | `ask_gate.refuse` in `project.json`: the project's own ask_user refusal rules (regexes, exceptions, a reason), enforced by the coordinator's gate with no runtime edit. See [references/ask-gate-rules.md](references/ask-gate-rules.md) |
| Chat-settable config | Which keys a coordinator `config_set` accepts (per-tier routing, the saved endpoint's alias; never a base URL): `ttp config --chat-settable KEY [VALUE]` checks one without setting it. See [references/chat-settable-config.md](references/chat-settable-config.md) |
| Setting holds | While `delivery.allow_protected_push_branch` refuses the push branch, landing tasks wait on the open ask with their attempts untouched. See [references/setting-holds.md](references/setting-holds.md) |
| Charter grants | The daemon (runtime `grants.py`) compares the charter's push and auto-merge grants with the delivery settings, model-free. See [references/charter-grants.md](references/charter-grants.md) |
| Live-impact mitigations | A hand-off's `live_impact` field queues a due, priority-1 workaround task beside the fix; overdue wakes the coordinator. See [references/live-impact.md](references/live-impact.md) |
| Charter guard | The coordinator's `charter_update` refuses added text a standing Restrictions item contradicts, and names the forms that pass. See [references/charter-guard.md](references/charter-guard.md) |
| Obligations | `ttp obligation`: a deliverable owed to the user, tracked on its owner tasks until a receipt. See [references/obligations.md](references/obligations.md) |
| Resource locks | `ttp lock <resource> -- <cmd>` (`ttp lock A,B -- <cmd>`: several, all or nothing), `--shared` for read-only commands, never on a device lock; opt-in `locks.pre_grant_check` asks a project command before each grant; one global lock order: narrower (host, device) before wider (site-wide) locks. See [references/resource-locks.md](references/resource-locks.md) |
| Shared watchers | A watcher that workers also run: non-blocking lock, re-check, dedupe, and seconds-fast git steps. See [references/shared-watchers.md](references/shared-watchers.md) |
| Machine change ends | `ttp machines change add ... --until-task <id>`: a ledger entry open while a dependent task is; `--auto-undo` for an idempotent, quick undo safe to run unattended. See [references/machine-change-ends.md](references/machine-change-ends.md) |
| Continue hops | A done task's `continue: ...` follow-up is queued at the parent's queued tier; `mechanical: ...` runs it light. A continue of a failed task starts at its predecessor's queued tier and a `Fix review` fix at standard, unless `user_deep` or a `needs_deep` hand-off or finding asks for deep; `tier_source` says why. See [references/continue-hops.md](references/continue-hops.md) |
| Tier floor | Light is for mechanical work only (file reads and copies, small scripted edits, polling, test reruns). A conclusion run (a `question` or `plan` task, a chat answer, a spec line starting `conclusion:`, a reporting llm schedule such as the daily review), a verdict run (a review, a spec asking for a verdict, pass/fail call, k/N table or cell results) and a device or machine run (`needs_device`, a device or machine resource, a spec ordering a reset, power cycle, soak or device job) starts at `coordinator.conclusion_floor` or above (default standard tier, high effort), after the effort pick and before a retry's tier-up; it never routes to the chat provider, a cheap pin that is no requirement is dropped, and required pins stay. Each lift sets `tier_source` `conclusion_floor` and a `[tier floor] ...` spec line. A work, code or harness spec whose output is a conclusion the user reads carries a `conclusion:` line. |
| Self-check before hand-off | A code worker tries to break its change before handing off (spec steps ticked, guard bypasses, unexpected exit codes and interrupted runs, bounded waits, no I/O in a db transaction, diff size) and hands off a `self_check` line; `review_stats.self_check` records whether the reviewed hand-off had one. See [references/self-check.md](references/self-check.md) |
| Superseded reviews | A failed or `changes_needed` review closes as superseded (verdict kept) once a later re-review in its chain passes, model-free; by hand, `ttp task <name> superseded <id> --by-review <passed re-review id>` or `ttp task <name> superseded <id> <covering commit> [--ref <ref>]`. No evidence, an unreachable commit or a failed or unrelated re-review is refused. Superseded reviews leave the open lists and the unhandled-failed-review check. |
| Evidence manifests | A hash-pinned manifest names only immutable artifacts and receipts, never a mutable queue, outbox or growing log; queued, recorded and delivered stay distinct. See [references/evidence-manifests.md](references/evidence-manifests.md) |
| A project's own checks on gh calls | Executables in `bin/gh.d/`, never edits to `bin/gh` (the template's, replaced on upgrade); they can only refuse. See [references/gh-pre-hooks.md](references/gh-pre-hooks.md) |
| A project's own tool guards | Python files listed in project.json `guards.local`, outside runtime/ and prompts/; never edits to `runtime/ttp/hook.py` (an upgrade conflicts on them). See [references/local-guards.md](references/local-guards.md) |
| Web app | `runtime/ttp/web/` |
| Runtime behavior | `runtime/ttp/*.py` (last resort) |

## Rules

- One friction, one small commit, a one-line reason in the message.
- NEVER loosen a restriction, cap or notification policy without the user.
- To change an executable script (`bin/`, hooks, check scripts), write the new version to a temp
  file in the same directory, keep its mode, and rename it over the old one; never edit a script in
  place while it may be running: a shell reads a script a piece at a time, so an in-place edit gives
  it half the old file and half the new (`line N: syntax error`). `ttp setup` and `ttp upgrade`
  replace harness files the same way. A check that fails on such a syntax error in a script outside
  the change is run once more; failing so again, it is an infrastructure fault, never the change's
  failure (references/push-checks.md).
- Runtime edits: keep Python 3.9-compatible; run `python3 -m pytest -q runtime/tests` if present;
  (pytest from the harness root uses its `pytest.ini`, isolated from any enclosing repository's
  pytest config and `conftest.py`, with `runtime/` on the import path; add local paths to its `pythonpath`)
  then `ttp restart <name>`. It waits for the daemon to tick; if it does not, `runtime/` goes back
  to the last version that ran (a new commit) and the user is alerted.
- Measure after: same evidence, a day later. Revert what did not help.
- Change only this project's own harness. Never edit, or create a worktree or branch in, the
  tt-project plugin's source repository or another project's harness. Upgrading another
  project on this machine to the installed release (`ttp upgrade <name>`) is not editing its harness; hand edits to its charter, memory, config, state or code are.
- A lesson every project would benefit from → an upstream note in the hand-off: a follow-up
  titled `upstream: …`. It is only for a defect or missing feature in the tt-project plugin itself
  (runtime, CLI, web app, prompts, skills): for example, a daemon that misreads a wait probe, or a
  worker prompt rule that leaves workers guessing. The project's own code, machines, models, test
  environment or its own harness scripts get a normal follow-up in this project: for example, a
  box missing a tool version, or a model output mismatch. The daemon files an `upstream:` follow-up
  that names nothing of tt-project (no word such as tt-project, ttp, daemon, harness, coordinator,
  worker, schedule, push, review or runtime) as this project's own follow-up, and logs it.
- Notes reach the project that reads them even when its machine cannot reach this one (a laptop
  behind NAT): each daemon sends this machine's own notes on over ssh to `ttp upstream --receive`
  there, with the user's keys and known hosts and no new port. Notes go only where they are read;
  one for a project (`ttp note --to`) goes only to the machine that runs it. A project this
  machine's registry does not know is looked up on the forward targets over ssh (`ttp upstream
  --has-project <name>` there); an unknown name fails naming each machine asked, and the
  `ttp note --reply <fp>` to use when that name sent this project a note. Each received note keeps
  its route (sender, its machine, the machine it came through) in the reading project's database,
  and its event ends with that reply command: a reply goes back the same way, also as
  `ttp note --to <project>@<machine>`. A machine that pushes notes here but cannot be reached from
  here takes the replies held for it on its next call (it asks at least hourly for a week after
  sending notes here); with no route a reply exits non-zero, not sent. `ttp upstream
  --forward-status` shows each target's cursor, last success and last error; `ttp upstream
  --forward-to <aliases>|default|none` sets the targets. Received notes stay untrusted data.
  A target the daemon has failed to reach for an hour shows in `ttp status` and `ttp note --to`;
  on macOS, repeated 'Network is unreachable' is read as a Local Network privacy block on the
  daemon's python, and `ttp doctor` names the fix. Past 30 min, `ttp note` in a run and `ttp
  status` send the pending notes from their own context (`ttp upstream --forward-now`), through
  the same cursor, so none is lost or filed twice. `ttp note --status <id>` (the id `ttp note
  --to` printed) says where one note stands: queued, filed-local, forwarded (in the inbox of the
  machine that runs the project; asked over ssh with `ttp upstream --note-state`) or delivered
  (that project's daemon read it). It exits 0 once delivered, 1 while on its way, 2 unknown.
- A `ttp note --to` past 4,000 characters (whitespace joined) is refused whole, never cut; a
  filed or queued one prints its stored length and a short hash. Put the request first. A hand-off
  `upstream:` note past the limit is cut with a marker saying how much was kept.
- Each task a `ttp note --to` names (`#<id>`) gets its live record appended, as fits the limit:
  `[facts at send time from <project>'s records: ...]` (status, landed or superseded commit, machine
  changes). A worker's prompt gets the same for the tasks its spec names, read at dispatch (`## Facts
  at send time`): where the text states their status, the records win.
- Closing a note: the maintainers record `ttp upstream --resolve <fp> --fixed-in <version>` (refused when the
  task whose hand-off lists the note in `resolves`, or `--task <id>`, has no landed commit in that version) (or `--wontfix "<reason>"` for a note that is not a
  plugin bug: closed, no release), or a
  code task's hand-off lists the note in `resolves` and the push queue records it with the version
  its batch pushed. A sandboxed `--resolve`, `--ack` or `--regress` is queued in the run's directory and
  recorded when the run ends. Work
  waiting on it hands off `waiting` with retry_when `ttp upstream --fixed <fp>` (0 once this
  project's running daemon has the fix). The daemon then tells the coordinator once, and the
  project closes the note with `ttp upstream --ack <fp> "<evidence>"` (or `ttp note --confirms <fp>
  "<evidence>"`). The fixing project reads that ack model-free as its receipt, so a worker never also
  sends it a note to confirm the fix; a new problem goes in its own note, without `--confirms`. A note
  sent with `--confirms` and `--to` is closed by the fixing project as "confirmation, not a bug",
  counted in its daily review, with no coordinator turn. With no ack a day after the
  resolution, it shows as "shipped, not confirmed"; the coordinator gets one reminder 72 h after
  the fix reached this project, and with no ack and no regression the note closes by silence after
  14 days. If the fix did not hold, run `ttp upstream --regress <fp> "<reason>"` (filer project only,
  after a resolution; stays on this machine): it blocks the close by silence. Filing the same note
  again after its resolution counts as a regression. `ttp upstream --mine` lists this project's notes.
  `ttp upstream` lists the inbox's notes with each sender (project, host, task), open first; `--resolve`
  prints the sender and sends the answer once to a sending project on this machine.

## Maintainer checklist (liveness, progress, deploys)

- Accept a deploy only through `ttp deployed`: every project's daemon reports the release by its
  own receipt. An installer exit, the files' version or a fresh heartbeat is not a deploy.
- Never accept "no tests collected" as a pass, and never blanket-skip a check. Fix where the
  check's applicability is declared; a related change that lacks its test still fails.
- Wait on an upstream fix with a probe (`ttp deployed`, a version check) as `retry_when`, never
  `blocked`. A hand-off is done only once the consumer acknowledged it and recovered.
- A task asleep on its `retry_when`: a spec update sleeps with it and applies when its wait ends
  (the probe passes or another wake), at the task's own tier, priority 1 too. An update that adds
  an `urgent:` line (once per wait) or a `task_update` with `wake_now: true` wakes once at once,
  unless another hold (a dependency, start_after, start_when, a paused or busy resource, a gate)
  still holds it. No watcher or extra wake task. `wake_now` may name a tier
  (`light`/`standard`/`deep`, capped at the task's own); requests before the run starts, or within
  30 minutes of the last spec wake, share one wake at the highest tier asked. Appended
  `## Obligation` notes are no spec change. A spec wake whose run hands back the same
  wait holds later spec wakes for `waiting.spec_wake_hold_s` (2 h) or until its retry time.
- See "Liveness, progress and deploys" in `docs/design.md`.

## Stored secrets never reach tool output

- Never print, echo or read back a value saved with `ttp secret` (no `credentials()` or secrets-store
  read in an ad-hoc command): the output stays in the run's log and transcript. The Bash hook refuses
  such reads.
- Test an endpoint's key with `ttp secret check <alias>` (reachable, auth ok or auth failed, the
  model ids it serves and any configured tier model it does not; never the key or URL). To pick a
  replacement model without the key, read the served ids `ttp status` shows. Give
  it to a command with `ttp secret env <alias> -- <command>` (`OPENAI_API_KEY`, `OPENAI_BASE_URL`).
- Store a value of your own with `ttp secret set <name> < file` (stdin only), or a set of fresh random
  tokens with `ttp secret generate <name> --keys a,b,c`; neither prints a value.
  Give one to a command with `ttp secret env --secret <name>[:<key>]=<VAR> -- <command>`.
  A built-in one goes only by a field: `--secret jev:key=<VAR>`. In the sandbox, `ttp secret env`
  has the daemon run the command; you get its masked output and exit code, never the value.

## CPU-only runs never open a device

- The daemon sets `TTP_NO_DEVICE=1` for a task run that holds no device (not tagged `needs_device`,
  no `*-device` resource or device lock), and `0` for one that does. Hooks can honour it.
  `ttp lock` on a device resource sets it to `0` for its command, and `ttp devq` runners and jobs
  never inherit it: those are the sanctioned ways to a device.
- For a pytest repo whose tests open a device through fixtures, ship `ttp_no_device.py` from this
  skill's folder: copy it next to the repo's root `conftest.py` and add
  `pytest_plugins = ["ttp_no_device"]`. Under `TTP_NO_DEVICE=1` it deselects tests that use a device
  fixture (globs, default `*device`) or marker (default `*hardware*`, `*device*`, `silicon`). Set
  the lists per repo with the `ttp_device_fixtures` / `ttp_device_markers` ini keys or the `TTP_DEVICE_FIXTURES` /
  `TTP_DEVICE_MARKERS` variables (comma-separated).
- Do not rely on `-m 'not device'` alone: a marker the repo does not define deselects nothing.
- `TT_VISIBLE_DEVICES` only asks: a raw open or reset code still reaches the device. Under
  `TTP_NO_DEVICE=1`, `ttp sandbox` also hides the device nodes (an empty tmpfs over
  `/dev/tenstorrent`), and on a host tagged `device` the worker hook refuses pytest and ctest runs
  outside it. On a Claude Code that applies a hook's rewritten input, a test run that is the
  command's last part (after `;`, `&&`, `||` or a new line) is run as `ttp clip -- ttp sandbox -- <cmd>`
  instead, with a one-line note. A power command sent over the network to a board management controller is not stopped.

## Test runs never reach other processes

- On a shared host a test suite's own process-kill code (cleanup by pid, `pkill` in a fixture) can
  hit other users' live jobs; the kill guard refuses such calls by name. Checks run unsandboxed by
  default. A project that sets `sandbox.checks: true` has `ttp checks`, `delivery.push_checks` and the
  push queue run every local check in a process sandbox: its own PID namespace and /proc, so it sees
  and can signal only its own process tree, and nothing it starts outlives it (sudo fails inside).
- A pid recorded inside a `ttp checks` run (`$$`, `os.getpid()`) is local to that run's PID namespace:
  to another check or the daemon the same number is another process or none. A gate or driver that
  other checks attach to holds an flock on a file in its folder for its whole life, probed with
  `flock -n` (`ttp detach --check <dir>` reads `<dir>/lock` so), or is started with `ttp detach`,
  which a check hands to the daemon. A gate that yields (exit 75) writes its exit code and stays
  queued, never deleted as dead.
- Run other test commands the same way: `ttp sandbox -- <command>` (a plain run unless the project
  turned the sandbox on, or `TTP_NO_DEVICE=1` hides the device nodes).
- A reaper or cleanup script signals only processes it can prove are this project's: `TTP_PROJECT`
  (the full path of `<root>/tt-project`) or a `TTP_RUN_DIR` inside it, or its own run record with pid
  and start time. Never by a task or run id in an env, command line, run folder name or log: ids
  overlap across projects. Skip processes still writing output.
- It needs unprivileged user namespaces and `unshare` or `bwrap`. Where the host allows none, checks
  run as before and say why; `ttp doctor` names the setting. Under `sandbox.checks: true`,
  `"sandbox": false` on a push_checks entry is only for a check that needs the host's process table,
  and is reported each run.

## Resetting a Blackhole Galaxy host

Company-wide practice; the long form is in the `tt-project:setup-box` skill's recovery reference (recovery.md).

- Reset a host's own chips with `tt-smi -glx_reset`, then rescan the root ports and recount the
  chips by BDF and tray. `tt-smi -r` is never used, not even as a fallback. Where glx_reset cannot
  run (no root, no NOPASSWD rule for it), no reset runs and the output says so.
- Check sudo rights with `sudo -n -l` (or `sudo -n -l <command>`), never `sudo -n true`: a host
  may grant NOPASSWD only for specific commands.
- Runs are unsandboxed by default: sudo, ssh and the host's network work as in a plain shell. Only
  in a project that turned its provider's write fence on (`providers.<name>.write_fence: true`) do a
  worker's sandbox and its `ttp detach` jobs not gain privileges ('no new privileges': sudo fails);
  `ttp lock` then refuses to wrap sudo. A privileged command the project allows then runs on the host through the daemon:
  `ttp host-run -- <command...>` prints its output and exits with its code (126 refused, 124 timed
  out). Only commands in config `runner.host_commands` run, each entry the command's first words
  (name them fully, e.g. `sudo -n /usr/sbin/<tool> <read-only verb>`); the user or the coordinator
  sets the list (`ttp config` or chat's `config_set`, each refusing a shell string or a bad entry by name), never a worker. Each run goes to `state/host-run.jsonl` with its task id. A plain
  entry runs under `runner.host_command_timeout_s` (at most 600 s); an entry
  `{"cmd": "<command>", "timeout_s": N}` sets its own limit, up to 3600 s (over it, or malformed:
  refused and named, never clamped); `--timeout S` asks for less. An entry with `"exact": true`
  (`{"cmd": "sudo -n <command>", "exact": true}`) runs only a request of exactly its words, so
  nothing can be appended: use it for privileged commands (`ttp doctor` warns on a plain sudo
  entry). Anything that may run past about 9 minutes (a tool call stops at 10):
  `ttp host-run --detach -- <command...>` returns its id at once and the run goes on without the worker; hand off `waiting` with retry_when
  `ttp host-run --status <id>` (0 ended, 1 running, 4 unknown or lost), then read
  `ttp host-run --result <id>` (its output, its exit code). A lock that must cover the whole run:
  `ttp detach <name> --lock <res> -- ttp host-run -- <command...>`, not `ttp lock` around `--detach`
  (which frees the lock once the run started).
- The keys that turn namespace sandboxing on (`providers.<name>.write_fence`, `sandbox.checks`) are
  user-only: only the user's own word turns one on (`ttp config` in their own shell, or the web
  settings page). `ttp config` inside any run, a worker's or a coordinator's, and every coordinator
  `config_set` refuse the on value, in a turn carrying a user message too; the off value (false, or
  `''` to unset) anyone may write. `restrictions.no_internet` also turns Codex's and Cursor's
  sandbox on and is a Restriction: a run's `ttp config` refuses it on or off, and `config_set`
  cannot set it. A user's
  harness-wide order (such as unsandboxed runs) applies to every project; a project never opts out
  on its own judgement: it records the workaround it relied on instead.
- A fenced Claude worker's (write_fence true) `ssh`, `scp`, `sftp`, `rsync` and `ttp lock` run outside the Bash sandbox
  only as a simple command on its own (also after `timeout N`); the hook refuses them beside `&&`, `;`,
  a pipe, a file redirect or a substitution. A project's own host-reaching command (a script in its
  harness `bin/` that wraps ssh) is freed the same way by config `sandbox.fence_freed_commands`
  (`["my-ssh"]`; the sandbox never frees `VAR=... my-ssh ...`, so an entry's `env` part is ignored), never
  by editing FENCE_FREED in hook.py or FENCE_EXCLUDED in providers/claude.py: those lines belong to
  the template, and a local edit there turns each upgrade that changes them into a merge task. A
  name that is not an executable in the harness's `bin/`, or is a shell, wrapper, `ttp`, `git` or
  `gh`, is left out with one daemon log line.
- In a sandboxed run (and its `ttp lock` and `ttp detach` jobs) nothing can gain privileges, so an ssh
  whose ProxyCommand runs sudo fails at once with `kex_exchange_identification: Connection closed by
  remote host`; the host is not refusing. `ttp ssh-preflight` exits 13 for it. Stay unsandboxed (the
  default), run that ssh from a model-free command schedule or pre-step, or use a ProxyCommand without
  sudo or a ControlMaster opened outside: see [references/ssh-preflight.md](references/ssh-preflight.md).
- A reset or a single-host power cycle never affects other hosts: nothing waits on or snapshots
  them first. Only a quad serving a quad-wide workload goes down and up as a quad.
- Neighbours, a `serving` tag or a site rule the harness infers never hold a reset, power cycle,
  run or install, nor make it an ask: only a user-set hold or allowlist (with the user's message
  as its source), a lock or device hold, a pause, a spend cap, a charter Restriction or a live
  tenant on that box does (setup-box recovery reference).
- After a tray power drop, try glx_reset first, then the single-host BMC power cycle with op 0x3
  only (op 0x0 also resets the PCIe retimers carrying NVMe). Tray temperatures read right after a
  power loss are junk.

## Rebooting the harness's own host

A reboot ends every run in flight, and nothing lifts a plain pause afterwards: schedules and probes
do not run while the project is paused. A task that must reboot the host its harness runs on:

1. `ttp drain <name> --wait 240`: no new runs or pushes start; it exits 0 once none is in flight
   (the calling run does not count) and 1 at the timeout, with the project left paused. Running
   workers are never killed. Keep the wait inside one tool call (a few minutes). On exit 1 run it
   again in further tool calls of the same run, at most 3 times. Still not drained: `ttp resume
   <name>` and hand off `waiting` with a time-based `retry_when` (`test "$(date +%s)" -ge <epoch>`).
   Never leave a paused project with a waiting task: probes do not run while paused, so it never wakes.
   Drain also lists the project's `ttp detach` jobs still running on this host: the reboot ends them.
   `--jobs` waits for them too. A chain that only drives another machine belongs there instead
   (`ttp detach --remote`), where this host's reboot cannot end it. A job started with
   `ttp detach --resumable` (only one that is safe to rerun from the start) is started again by
   the daemon after the reboot while its task still waits on it, with no model run, up to
   `budget.max_job_relaunches` times (default 3). Abrupt reboots it relaunches through count toward
   `budget.max_reboot_losses`, so a job that takes the host down still gets its task blocked.
   A task that held a resource through `ttp lock` (a device, a machine, a build directory; not a
   push lock, and not a lock named after another listed machine) when the host went down abruptly
   and unplanned is blocked at once, for its run and for its waiting detached jobs: no retry, no
   relaunch, no boot-time wake. Its high `task_blocked` event names what it held; requeue it once
   its earlier attempt is known not to be implicated. Reboots where it held nothing resume as before.
2. `ttp pause <name> --until-reboot --reason "host power cycle"`: the daemon lifts this pause on its first tick after the host
   booted again and says so in the feed. A plain `ttp pause` or `ttp resume` replaces it.
3. Write the hand-off (`waiting`, with a `retry_when` that holds once the host is back: the daemon
   probes it after the boot and wakes the task when it passes), and start the reboot a little later
   (for example `shutdown -r +2`), so the run ends cleanly first.

A planned reboot or power cycle (a recovery owner cycling the host on purpose, for any project on it):
add `--reboot "<why>"` to the drain. It records the cycle in `state/host-cycle/<id>/record.json`
(`boot_id`, `started`, `reason`, `dry_run`, and the detached jobs it interrupts). On the next boot
every project here reads the records of all projects on this host: a detached job that a matching
cycle (not a dry run, started at most 2 h before the boot) cut short is not counted toward the
task's `budget.max_reboot_losses`, and the boot notice and the woken task's next run say it was planned.

`--reboot` also holds the whole host, not only this project. As it starts, the drain writes a
power-cycle intent for this host into the shared machine ledger (`ttp machines`), timed at the end
of its wait and holding from now; when an intent made on this boot is already due, it takes that
one up instead, pushing it to the end of its wait if it was due sooner. While the intent is due,
every project's daemon on this host starts no new worker or reviewer run and no push-queue push,
and `ttp checks` exits 75 instead of starting; coordinators still run, and workers already running
get the usual notice to hand off. `ttp status` and the digest's paused section show the hold. The
drain's wait and its list also cover the other projects' runs and pushes on this host (their
coordinators are listed, not waited for); its exit codes do not change. Once the drain records the
cycle, the intent's time moves to then. Other projects are held only when this host is in
`ttp machines list`: the ledger is keyed by its alias there. The hold lifts at the first of: the
host booting again; 10 min after the intent's time (the cycle did not come: each project posts one
low feed line, and the ledger entry itself closes 1 h after that time); or the intent closing, when
the drain that wrote it records no reboot (`dry_run`, or `rebooted` false) or `ttp resume <name>`
withdraws an intent this project's drain wrote on this boot, no other project's drain took up, and
no record names (it prints `planned host cycle withdrawn`).
A drain that only took up another project's intent never closes it, since that cycle is still
coming: once it and every later drain that moved the intent record no reboot, the intent gets back
its own time and window.

A recovery tool of its own may write the same record itself, and must fsync it (and any other state
it writes) before the cycle: on some filesystems a record written seconds before a power cycle is
lost otherwise. Such a helper pauses the project with `ttp pause <name> --until-reboot --reason
"host power cycle"`, never a plain pause: a plain pause outlives the cycle and nothing lifts it.
`--until-reboot` without `--reason` records the reason "host power cycle (until reboot)".

The boot is told by the kernel's boot id (on macOS, kern.bootsessionuuid, else kern.boottime); where none can be read,
`--until-reboot` is refused.

## Template updates

- `ttp upgrade <name>` merges the installed tt-project template in a scratch worktree, checks the
  runtime compiles and imports, then fast-forwards this harness. It settles conflicts that need no
  judgment itself (both sides only added at one spot, or upstream already ships the change).
  Otherwise the harness is left as it was and a harness task is queued to finish the merge, at
  most one per `upgrade.task_every_h` (6) hours. Keep this project's own imports in a runtime file
  in one block at the end of the file, after its `# --- project-local imports below ---` line where it
  ends with one, outside upstream's `from . import ...` lists, so upgrades merge cleanly with no task. It then exits 75 (deferred, not failed) while that task or the daemon's retry under
  `upgrade.auto` takes it on (the daemon retries only a newer version); when nothing does, it exits 1.
  The same conflict deferred past 48 h, or after two harness tasks that did not land it, is
  reported once as a normal observation.
  A finish task blocked or idle for 6 h no longer holds the harness against a host-side
  `ttp upgrade <name>`: that cancels and fences it (its `--apply` is then refused) and goes on.
- Conflicts: keep this project's intent, take upstream fixes. Resolve them in a scratch worktree,
  never in the live harness, and apply the result with `ttp upgrade <name> --apply <commit>`.
  Where the service manager is out of reach (a sandboxed worker), it asks the running daemon to
  restart itself (state/restart.request) and rolls nothing back while the old daemon still ticks.
  Exit 76: applied, restart deferred to the daemon (wait for restart.request to go); exit 75: busy
  (the daemon is landing harness worktrees or another upgrade runs), nothing was changed, so retry
  later and never wait on restart.request; exit 1: the restart failed or was rolled back.
- One merge at a time: `ttp upgrade` holds the `harness-upgrade` lock for the whole merge, refuses
  while another upgrade runs, and stops if `main` changed the template's files meanwhile. An open
  upgrade task holds the live harness until it ends: a newer release moves `upstream` and retargets
  that task (exit 75) instead of queuing another. The harness repo refuses any other merge of
  `upstream` into `main`.
