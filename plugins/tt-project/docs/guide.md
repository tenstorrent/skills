# tt-project guide

How tt-project works and what you can tune. Install and first steps: [the README](../README.md).
Install details, updates and uninstall: [install.md](install.md).

## Using it

| You say | What happens |
|---|---|
| "Start a tt-project called X on box B: <brief>" | the project is created on B and starts working |
| "Connect to project X" | this chat attaches; replies and alerts arrive here |
| anything addressed to the project | relayed to the coordinator verbatim (the chat's own context goes apart, in `ttp say --context`, or alone in `ttp say --finding`, and counts as your word only in a project where you turned on `coordinator.agent_orders_as_user`, never for a PR's ready or merge, money, credentials, a step that could affect other people or speaking as you; a Restrictions change it orders is cited relayer-ordered and listed in the daily review); its answer comes back to this chat |
| "Tell all projects: <words>" | `ttp say --all`: every project in `ttp list` gets your words once, marked as a broadcast, so none relays them on; a resend within 24 h reaches only the ones that missed it |
| "What is X doing?" | `ttp status X`: running work, blockers, spend, why idle |
| "How are all my projects?" | `ttp overview` (or the web app's Projects tab): one line per project on this machine |

The brief can be inline text, a file, or links. Goals, restrictions ("never access the internet")
and preferences you add later become part of the project's charter and memory.

From a shell inside a git repository, `ttp new demo --describe "Keep the README accurate."` creates a
project and ends with a link to its web app, `http://127.0.0.1:<port>/#token=<token>`; `ttp web demo`
prints it again. `ttp say demo "<your words>"` and `ttp status demo` talk to it without a chat: a
`ttp say` without `--chat` keeps one reply chat per caller (the calling project, else the host). Each
such say first prints that chat's unread replies (up to 20, oldest first) and marks read only the ones
it printed, then sends; `--wait <seconds>` (at most 300) also waits for the answer to the message just
sent. A failure to read replies is reported on stderr and never stops the message. `ttp listen demo
--chat <id> --once`, printed after each say, reads the chat without sending.

## How it works

```
 chats (any host) ─┐                ┌─ worker: headless agent in its own worktree ─┐
 web app ──────────┼─ inbox ─► daemon ─► coordinator turn (decides, never works)   ├─► results
 watchers ─────────┘   (deterministic)  └─ worker … (parallel, budgeted, stall-guarded)┘
```

- **Daemon** (one per project): schedules, watchers, budget gates, message routing, web app.
  Deterministic and cheap. It starts a model only through a run.
- **Coordinator**: always the project's hands-on manager: one persistent session (Claude Code,
  Codex, Cursor), woken by chat, task ends and timers, that corrects workers mid-run and fixes the
  project's own prompts, scripts and config, while heavy work still goes to workers. No setting
  turns it into a stateless turn; a daemon restart or host reboot resumes the same session, a full
  context compacts in place, and only a resume the provider rejects (or a provider-side expiry)
  hands the context to a new session. Only the chat provider, which keeps no session yet, runs a short, tool-less stateless
  decision over a digest of the project. It reads orders by their intent (a Restriction binds
  as written), decides on its own unless a step could hurt people or speak or pass as a human,
  and asks rarely, never waiting on the answer.
- **Workers**: one task each, in a git worktree on their own branch for code, with a dollar
  budget, a wall clock, and a stall guard. Each hands off a structured result. A worker whose
  machine or queue is busy hands the task back to retry later, instead of waiting inside the run.
  The wall clock and stall guard count only time the host is awake. A run a host sleep cut short
  is retried without spending an attempt and is not counted as waste. After a wake, nothing new
  starts until the host has been awake for `budget.wake_settle_s` (default 300 s), so a laptop's
  brief maintenance wakes start nothing. Messages from people are still answered at once.
  A run a reboot cut short, or whose process died without a result (a crash), continues its agent
  session in the same working directory with a short prompt, instead of starting over, and spends
  no attempt (a second crash in a row counts one). A run a sleep or the network cut short does so
  after real progress (`budget.resume_lost`, default $0.50 or 10 min; `false` turns resumes off).
  After a reboot the daemon also starts again, once per boot and model-free, a task's own detached
  `ttp checks` and a review's precheck (only while that review is open), on the same head and at
  their old place in the host's checks queue. A task deferred since its hand-off (`start_when`,
  `start_after`) gets nothing restarted; its start gate decides. A resumable `ttp detach` job is
  relaunched even after a probe marked it lost to the reboot, or when the wait is `survives_reboot`;
  each relaunch not made says why in daemon.log. The Host line counts runs cut short as resumed
  (restarted, with or without their work) or not restarted.
  A task lost to `budget.max_reboot_losses` abrupt reboots is blocked, as it may cause them. So is,
  at once, a task whose run or detached job held a `ttp lock` resource of this host (a device, a
  machine; not a push lock) when the host went down abruptly: the coordinator requeues it once the
  attempt is known not to be implicated. A
  detached job cut by a planned host cycle (`ttp drain <name> --reboot "<why>"`, or another project's
  `state/host-cycle/<id>/record.json` on the same host, or a `ttp machines change add <this host>
  --power-cycle-at <t>` intent) does not count. Such a reboot is reported once as planned, at low
  severity, naming what planned it; only abrupt, unplanned reboots count toward the "host looks
  unstable" warning.
  When the coordinator rescopes a running task, the change reaches the worker mid-run.
- **Memory and charter**: plain files in the project's harness, one fact per file. A temporary entry carries its end (a time, a plain-language condition or a probe); the daemon retires it to the archive or `CHARTER.history.md` once that passes and says so.
- **Watchers**: pull requests (CI, reviews, mergeability) and logs, reporting only changes.
  With Jev enabled (see [Jev](#jev-optional-add-on)), new observations are screened by a cheap
  decision model first.

`ttp issues list` shows a project's open daemon and watcher issues; `ttp issues close <id-or-key>
--why "<reason>"` closes a stale one that nothing closes by itself (say a schedule gap after a host
move), with the reason and time in its history and the activity feed. A condition that is still
live reopens the issue at its next detection.

Keyed alerts keep their history when closed. `ttp alerts <project>` shows their message IDs;
`ttp alerts <project> --ack <id> --why "<reason>"` acknowledges that message's episode with the
reason. One whose condition is over closes; one that still holds moves to the feed and stays quiet
until it clears, so a new occurrence alerts again. A live outage (logout, unreachable hosts, full
disk, machine or whole-box heal outage) cannot be acknowledged away. Retrying preserves the
original receipt, and an unknown ID is refused. A heal check's
user alert also clears when escalation moves to the coordinator or a known fault covers its
current failure; the check remains unhealthy until it passes. Known faults never silence a
prolonged whole-box outage.

An alert with no condition key closes at once when acknowledged by the id `ttp status` and the web
app show, and stays out of later chat delivery. The deploy rollback alert ("the daemon did not
start after a runtime change") and a failed requested restart carry the key `redeploy:<project>`:
they clear by themselves once a later deploy on that host is verified running, the facts
`ttp deployed` reads (every project there runs the installed release, any version, and this
project's daemon started after the alert). Other one-shot deploy and upgrade lines (a stuck
upgrade merge, a restored release, an upgrade recovered at start) are below high, so they appear
only in the feed and never under "needs you".

Each project also runs an overseer (`ttp.overseer`, its own service with `Restart=always`) beside
the daemon. It shares no process, lock or tick step with it: once a minute it reads the database
read-only (a short busy timeout, never waiting on the daemon's writes) and the run folders, and
judges the tick loop by its results, not its heartbeat: the last dispatch, urgent work ready with
a free slot, finished runs not reaped, coordinator answers not applied, events not delivered and
slow tick steps per hour. That reading is one `loop health:` line in every coordinator digest,
`ttp status` and `ttp deployed`. Urgent work waiting with a free slot, a run unreaped or an answer
unapplied for over 5 min, or a stale heartbeat, is a results failure: the overseer saves the
daemon's stacks under `state/incidents/` (py-spy when installed, else the daemon's own stack dump)
and records one incident in that line until the results are good again. The daemon restarts an
overseer whose heartbeat is over 5 min old. Idle cost: no model call, a few read-only queries a
minute.

The coordinator decides when it looks next: a `look` action sets a time, what to check and why,
and optionally a shell check that skips the wake when it exits 0 (nothing changed). While a look
is pending it replaces the fixed idle and idle-slot wakes; with none, those come back. A due look
starts a turn in the same session. If the tick loop leaves a look pending 3 min past due, the
overseer starts that turn itself (at most one every 10 min); the loop and the overseer share one
start lock, so they never start two turns. A tick step over its budget in 3 of its last 10 runs
sits out the next 10 ticks (never the steps that reap, start turns, dispatch or deliver). It is
one incident in `state/overseer-incidents.jsonl` with the daemon's stacks, and a look wakes the
coordinator with that evidence.

## Budget

- Subscription plans: a plan's capacity is lost at each reset, so the project uses it. Below the
  line (90%, `100 - budget.reserve_pct`) it runs all its parallel workers; it does not spread use
  evenly over a window. Running work keeps using the plan after it starts, so near the line it
  estimates what each worker will still add (the account's measured burn per worker, times how
  long a run usually lasts here) and runs only as many workers as fit under the line: fewer in the
  last stretch, none new while those are all busy or the running ones alone would reach it. At the
  line nothing new starts until the window resets; the rest stays yours. Running work is never
  stopped.
- The 90% line is the default (`budget.reserve_pct` 10). If you share the plan with your own
  interactive chats, the project may use all but 10% of each window, so only that 10% is left for
  you. To keep more, raise the reserve: `ttp config <name> budget.reserve_pct 30` keeps 30%.
- Free Codex window resets (an account's `rateLimitResetCredits`) are used automatically, at most one
  per window, earliest-expiring first: when a window holds work at the line and resets more than
  `budget.reset_credit_min_hours` (12) from now, or when a reset expires within
  `budget.reset_credit_expiry_hours` (48) and a window is at least `budget.reset_credit_expiry_use_pct`
  (50) % used. Projects on one machine sharing an account never use two for one window.
  `ttp config <name> budget.reset_credits_auto false` turns it off.
- In that last stretch deep tasks run at the standard tier. `ttp status` and the web app say when
  the line holds new starts back; it clears by itself when the window resets. Budget alerts come
  only for what only you can clear (a dollar cap you set, funds), once it still holds new work two
  minutes on; they are withdrawn once it no longer does.
- Usage-billed accounts: $100 per 24 hours per project by default (`budget.daily_usd`); the
  7-day cap (`budget.weekly_usd`), when unset, is 7 times the daily cap in force ($700 by default),
  so it never stops work before the daily cap would. `ttp config` and `ttp setup` warn when a weekly
  cap you set is below the daily cap. With `budget.day_start` set, the daily cap counts
  the budget day instead of the last 24 hours. A new run
  starts only if its budget fits in what is left of both caps, each over that same window. At
  `budget.cap_yellow_pct` (60 by default) of a cap the gate turns yellow and halves the worker slots. A plan account whose successful
  runs stop reporting plan windows falls under these caps too (failed or silent runs do not count).
  The caps count only spend made on an account that was billed by use at the time: after a switch
  from a plan account to a usage-billed one, the plan's earlier spend stays out.
- Declared billing: `providers.<name>.billing` is `auto` (the default: plan or usage is judged
  from the plan windows each run reports), `plan` or `usage`. Set `plan` for a provider on a flat
  plan that reports no windows (for example a Cursor subscription): it is not held by the dollar
  caps, the runaway guard counts its runs instead, and its spend shows as plan, not billed. Set
  `usage` to keep the dollar caps on even while plan windows are reported. A change raises one
  budget-kind event and releases holds the old kind set. Cursor's account is a hash of the login
  its CLI status names (`cursor:<hash>`, never the email), or `cursor` when the status reads logged
  in but names none. While the status says logged out or cannot be read, the last label seen
  stands; before any is seen (after a restart) the account is unknown, so neither is an account
  switch.
- Daily spend target (a goal, not a cap; unset by default): with `ttp config --account
  budget.daily_target_usd <usd>`, `ttp status` shows today's Claude spend against it, and the daily
  review's self-efficiency audit (`ttp audit`) lists each project on this machine with the last 24 h's
  spend by class (coordinator, reviews, worker runs that ended waiting, other workers), then the total
  against the target. Each figure is real spend (a Claude run's own tokens priced at the model it
  actually used) with the booked figure beside it where they differ.
- Global daily cap (usage-billed, $200/day by default): `budget.global_daily_usd` stops new work in
  every usage-billed project on the account once the whole account's tt-project spend today reaches
  it. "Today" is the rolling 24 hours unless `budget.day_start` is set. Change it for every project
  on the machine with `ttp config --account budget.global_daily_usd 500` (or for one project with
  `ttp config <name> budget.global_daily_usd 500`); 0 turns it off. Plan accounts are bounded by their
  windows instead and never meet it. Running work and replies to your messages go on; it clears by itself at the day's reset
  (or, without a budget day, as spend leaves the last 24 h). Only spend on the same provider and the same account counts (an account is compared by a
  hash, so its name never leaves the machine); rows with no account recorded count, to be safe.
  The total covers every project `ttp list` shows on this machine, the machines of the registry's
  projects elsewhere, and machines tagged `tt-project` in `ttp machines` (`ttp machines add <alias>
  --tags tt-project`). Other machines are asked over ssh (BatchMode, no prompts) with
  `ttp spend-today`, at most every 10 minutes; their answers are cached in
  `~/.tt-project/global-spend.json`. A machine not heard from for 30 minutes is shown as stale with
  what its last answer for the same day would add, and is not counted: stale data never trips the cap. A machine that can reach another but not the
  other way round (a laptop that can reach a server, but not the server the laptop) pushes its own
  `spend-today` there instead: with the global cap on, each machine sends it over ssh to
  `ttp spend-today --receive` on every machine it asks, every 10 minutes (every 6 hours to a machine
  that says it already asks this one; less often after failures), in the background. Pushed answers
  are kept in `~/.tt-project/global-spend-pushed.json` and count like asked ones, stale after 30
  minutes the same way; a machine both asked and pushing counts once, with its newer answer.
  Each answer, asked or pushed, also carries that machine's estimate of its other Claude Code
  sessions (`other_sessions`: provider, account hash, dollars, `estimated: true`, and `billing`; `null`
  while its logs are not read yet). The estimate prices each Claude model call in
  `~/.claude/projects` at list price once, skips non-Claude models (a provider that runs Claude Code
  against another vendor's endpoint) and leaves out every tt-project run of any provider, which its
  project already books. Finished tt-project Claude runs calibrate it: once at least 3 runs mostly
  on one model agree within 5% on the ratio of Claude Code's reported cost to the list price, later
  calls on that model are scaled by that ratio (recorded in `~/.tt-project/price-calibration.json`,
  dropped when the model's list price changes). A ratio outside 0.5-2.0 is never applied. A low
  alert says what was done, once per model and price-table row: a drift past 20%, a ratio out of
  bounds, or a model the table does not list (priced at its most expensive row). `billing` comes from that machine's Claude Code login, never its name:
  `usage` (an API key, a key helper, or a usage-billed seat), `plan` (a subscription plan) or
  `unknown`. An API key is seen in the environment, the settings, or, for a key exported only in an
  interactive shell, the login's approved-key record; such a key beside a plan login reads `unknown`
  (it overrides the plan only while still exported). The session logs do not say how a call was
  paid. Only `usage` sessions count, once per machine with its answer. Plan sessions are shown
  apart as "subscription plan" and unknown ones as "estimated, not billed"; neither ever counts. An
  answer from an older tt-project has no such field, and one from a newer tt-project may carry a
  billing word this one does not know: either way its sessions count 0 and are shown as unknown,
  and its billed project rows still count.
  The budget line, `ttp status` and the web header show what is not counted after the total
  (`; not counted: $X subscription plan, $Y estimated, not billed, $Z stale`).
  `budget.push_spend_to` (account-level) names the machines to push to instead, or `"none"` turns
  pushing off. If the total cannot be worked out at all, the
  per-project caps stay in charge. Web, desktop and cloud sessions are not seen; another
  source can be added in code with `globalcap.add_other_source(fn)`, where
  `fn(provider, account, start, end)` returns `(usd, label)` or `(usd, label, billing)`.
- Budget day: with `budget.day_start` set (`"HH:MM"`), "today" is a fixed day starting at that time
  in `budget.timezone` (an IANA name such as `Europe/Berlin`; unset, the project's home zone, below;
  the host's own zone plays no part), 23 or 25 hours long across a daylight-saving change. The daily cap and the global
  cap count that day. Empty (the default) keeps the rolling 24 hours. Weekly caps stay rolling.
- Schedule caps: `ttp schedules` shows an llm schedule's suggested daily cap next to its own (runs a day x
  the median cost of its last 10 runs x 1.25; none under 3 runs), and the daily review lists caps under it.
  Past 80% of its own cap with a green gate, a schedule raises one `schedule-cap-80:<name>` observation per
  budget day, closed at the day's reset. Caps never change by themselves.
- Home time zone: each project keeps the IANA zone of the user's workstation as `home_timezone` in
  its `project.json`. `ttp new` records the zone of the machine it runs on (with `--host`, the
  workstation's, never the box's). `ttp connect` from a workstation (not from an ssh login), and a workstation's spend push
  for the projects it set last, send its current zone; a change is logged once in the feed, so the
  zone follows a travelling user. A project without one gets the account's `budget.timezone`, else the
  machine's zone, once at daemon start. Times shown to the user use it (`runtime/ttp/timefmt.py`).
- These keys (and `budget.push_spend_to`) are account-level: `ttp config --account KEY VALUE` writes them to
  `~/.tt-project/settings.json` (mode 0600), which every project on the machine reads under its own
  `project.json` (a project may still override a key, except `budget.push_spend_to`, which is read
  from the account settings only). Four `providers.chat` keys are account-level too, read by their
  own code rather than merged into each project's settings: `model_ranking`, `ranking_source`,
  `ranking_every_s` and `ranking_key_file` (see the chat provider's model ranking below).
  `ttp config --account KEY` reads one; an
  empty value removes it. The same holds for a project's own keys: `ttp config <name> KEY ''` removes KEY
  from `project.json`, so its default applies again (it prints the value it removed and records the removal
  in provenance, `--why`; a key the plugin no longer knows is removed too when `project.json` holds it, while
  a value for one, or removing one not there, is refused as unknown or retired; `ttp upgrade` drops the keys the
  plugin retired, such as `coordinator.unblock_effort`, by itself), and a key whose default is a list or an object
  refuses a value of another type without writing it, except the plain forms its reader takes
  (`budget.compact_window_tokens` and `budget.split_reread_tokens`: one non-negative number for every tier;
  `budget.resume_lost` and `waiting.wake_gate`: `false`, off; `coordinator.conclusion_floor`: a string such
  as `standard/high` or `off`; `providers_allowed`: a comma-separated string; `coordinator.provider_failover_to`
  and `ack_wait.probe_commands`: one string). The coordinator's `config_set` and the web app's
  settings do the same: `''` removes the key and a mistyped list or object is refused, writing nothing.
  Removing a user-only key (`config_set` outside the user's turn) is refused unless its default is the off
  side, and `config_set` still never empties `delivery.auto_merge_repos`; chat routing keys
  (`providers.chat.endpoint`, `providers.chat.tiers.*`) keep their own checks for `''`. `ttp spend-today [--json]` prints this machine's tt-project spend for the
  budget day by provider and account hash, and its other Claude Code sessions as estimated; it is
  what other machines ask for.
- Work backs off in steps as spend rises, pauses at the cap, and tells you how to raise it.
- A runaway guard pauses a project whose hourly spend jumps far above its own norm.
  It has no coordinator dollar limit (`budget.hourly_coordinator_usd` is retired): the always-on
  coordinator optimizes its own cost inside the dollar caps. Its coordinator signal (on a plan, too
  many self-started turns or turns on the same evidence) throttles the coordinator only: its self-started turns wait until the
  trailing hour is back under the limit, then one turn takes everything queued meanwhile. Your
  messages and must-act items still wake it at once, and worker tasks keep starting under the normal
  caps. `ttp status` says so on its coordinator line.
- A coordinator turn that goes over `coordinator.turn_budget_usd` is never retried as is: its batch is
  halved down to single items; a single item runs once more alone at the ceiling
  (`coordinator.turn_budget_ceiling_usd`, 0: twice the cap, never above
  the dollar caps). One that fails even then is parked and you get one alert naming every parked
  item; it runs again once you raise the cap or the ceiling. The other items go on.
- High events that arrive close together share coordinator turns. The first one after a quiet
  spell wakes the coordinator at once; high events within `coordinator.burst_window_s` (120 s) after
  that turn ended start no turn of their own and are read together by one follow-up turn when the
  window ends, so a burst costs at most two turns. They stay queued in the database, so a daemon
  restart inside the window keeps them. User messages (answers to asks too), critical events and
  safety-class events (spend caps, restrictions at risk, data loss, device or machine holds and
  pauses) never wait: they start a turn at once, with the held events in it. 0 turns it off. The
  follow-up turn's run note lists the held events under `burst`.
- A review runs light when the diff it checks touches no `review.risky_paths` glob and no code file
  with a risky name (`review.risky_names`: state, database, schema, migration, push, budget, billing,
  spend, spend cap, release and upgrade files by default; `[]` turns it off), and is docs and tests
  only or small (`review.light_max_lines` non-doc lines, default 80); standard otherwise. Every re-review after a
  failed review and every retry runs standard; only the coordinator picks deep. An optional
  `review.light_paths` glob list narrows light further: every non-doc file must match one of its
  globs (unset or empty: any path may go light). The review run's note records the pick and the
  rule behind it (`review_tier`).
- A code spec expected to change more than about 100 non-test runtime lines is split into
  independently reviewable tasks, each with its own tests: in measured runs such diffs failed
  review about twice as often as those under 100. The daily review's `reviews finished` line counts reviews and
  failures by all diff lines and by non-test lines (tests, docs and changesets left out).
- Local tool guards (`guards.local`, empty by default): the project's own rules on tool calls, the one
  supported place for them. Never edit `runtime/` files for a guard: an edited `runtime/ttp/hook.py` makes
  `ttp upgrade` conflict and leaves the project on an old release. List Python files by path from the
  harness folder, kept outside `runtime/` and `prompts/` (for example `guards/no_prod_restart.py`), so an
  upgrade never touches them. Each defines `refusal(cmd)`, asked about every shell command, returning the
  reason it refuses or None, and may define `tool_refusal(tool, tool_input)` for other tool calls (file edits,
  connector calls). Every provider path asks them after the built-in guards (the Claude, Codex and Cursor worker
  hooks, the runner-side guard of runs without one, and a session coordinator's tool check); a refusal blocks the call and is logged with kind `local:<file>`. A listed guard is never skipped: a
  file that is missing, does not import, has no `refusal` or raises refuses the calls it checks, naming itself
  and the error. Only a harness task edits a listed guard file. `ttp doctor` shows each guard and whether it
  loads. See the harness skill's references/local-guards.md.
- Hand-off checks (`worker.handoff_checks`, unset by default): the project's own model-free commands
  the daemon runs on each worker's hand-off before it accepts it, posts its `reply_chat` reply or
  queues its review or push. Each entry is `{"name", "run", "timeout_s" (60), "kinds"}` (`kinds`: the
  task kinds it applies to, default all), for example
  `{"name": "done says what it verified", "run": "python3 checks/handoff.py {result}", "kinds": ["work"]}`.
  `run` runs from the project root with the hand-off's path in `TTP_RESULT_JSON` (and `TTP_TASK_ID`,
  `TTP_TASK_KIND`), also put, quoted, in place of `{result}`; nothing else from the task reaches the
  command line. They check done, needs-review, blocked and failed hand-offs, not waits. Exit 0 passes.
  Exit 1 sends the run back at once with no attempt spent: the next run is told the check's name and
  the last 2 KB of its output, and nothing is posted or queued until a hand-off passes. After
  `worker.handoff_check_max_sendbacks` send-backs (default 2) the task goes to review with its result
  and branch kept, and its one coordinator event names the check and its output. Any other exit or a
  timeout is a broken check: the hand-off is accepted and the coordinator gets one
  `handoff_check_broken` event per check and failure. `ttp config` reports malformed entries.
  In Claude Code runs the worker's Stop hook runs the same checks first: exit 1 refuses the stop with
  the check's output, so the same session fixes it rather than a new run, and each refusal counts as
  a send-back. There the command also sees `TTP_HANDOFF_CHECK_AT=stop`, and the checks get 120 s in
  all; a broken or slow check lets the run end, and the daemon's run of the checks stays the final gate.
  The plugin ships one: `{"name": "leftover-procs", "run": "ttp leftover-procs {result}"}` sends a done
  hand-off back while a process runs in the task's own worktree (its resolved working directory is
  `tt-project/worktrees/t<id>` of this project or below it, so another project's task with the same
  number, or t390 for t39, never counts); a pid the hand-off lists in `leftover_procs`
  `[{pid, stop, ends}]` is left out. A project check that matches processes itself should compare
  the full resolved worktree path the same way, never a bare `t<id>`.
  A harness task's checks also get the daemon's verdict on whether its commits land:
  `TTP_HARNESS_LANDING` is `merged` (in the harness HEAD), `lands` (on the task's branch
  `ttp/harness-t<id>`, which fast-forwards the harness HEAD: the daemon merges it when the run ends),
  `none` (a `commit` of `none: <why>` with no commit on that branch the harness HEAD lacks: a valid
  no-change hand-off) or `refused` (no harness commit named, one on neither line, a `none:` hand-off
  whose branch still holds unmerged commits, or a branch that does not fast-forward),
  with `TTP_HARNESS_LANDING_WHY` saying what the worker can do. A harness-landed check should fail only
  on `refused`: a worker's sandbox cannot write the harness checkout, so it can never merge there itself.
  The merge is made in a scratch worktree; the harness checkout then only moves forward to it, the
  ref first (a refusal, such as the template guard's, writes no file), and git updates only files that
  still match the old HEAD. Land never runs `checkout --`, reset, stash or merge --abort there. An
  uncommitted copy (changed, staged or untracked) of a file the merge changes lands as it is only when
  it already holds the merged content: it is staged, never rewritten. Any other such copy (other
  content, an earlier branch version, a deletion, a symlink) stops the land and is named once per
  branch head; every uncommitted file is left alone.
- A task that has ended (done, failed, cancelled, exhausted) can still leave processes behind. Its own
  ones are those of your user whose environment carries this project's `TTP_PROJECT`, the task's
  `TTP_TASK` and a `TTP_RUN_DIR` of one of its recorded runs. When such processes are the only ones with a
  held lock slot or lane open, the daemon ends them (SIGTERM, then SIGKILL after 5 s) two minutes after the
  task ended, so the slot is free again, and writes a `leftover_procs_stopped` receipt to the feed.
  `ttp leftover-procs --stop <task> [--pid N ...] [--dry-run]` does the same on request, for a worker or the
  coordinator. It refuses a running task's processes, another task's, project's or user's, a
  `ttp detach` job's (use `ttp detach --stop`), one that uses a device, and any it cannot attribute.
  The daemon leaves processes the hand-off lists in `leftover_procs`; the command stops those too.
  Neither ever stops the project's live daemon. A `ttp checks` command (whose `TTP_RUN_DIR` is its
  check-run folder) counts as its task's. A stopped process that leads a process group takes the
  group with it, unless a member is protected or another task's, project's or user's: then only the
  leader goes and the command says why (exit 2).
- When delivery has a review step (`delivery.review_before_pr` or a `delivery.push_branch`), the
  daemon queues the review of each finished code task, and of each finished work task whose branch
  changes anything, in the same step that records its hand-off (`review.auto`, on by default). One
  rule decides the split, the review, the no-new-commits check and the continue hop for both kinds.
  A gap: a question or plan task (also a `continue:` hop that kept such a kind) never gets an
  automatic review, even when it committed; its unpushed branch reaches the coordinator as a
  local-only notice, and the coordinator queues its review or a code task for it. A review whose head carries commits of another task whose latest
  review failed, while that task's fix or re-review is still open, is queued but held: its status
  names the open fix, and it starts once that fix lands, once the fix chain ends failed or
  cancelled (its spec then says so), or after `review.carried_hold_max_h` hours (default 24, 0 no
  cap). A hand-off with no follow-ups, notes or findings starts no
  coordinator turn; any other still goes to the coordinator for those, and the review's spec lists
  its follow-ups. As a backstop, a done code task (or one waiting on something else, a busy lock
  too, for `review.unreviewed_wait_s`, default 1800) whose head is on no push-branch ref, with no review, open continuation,
  push-queue entry or recorded reason (superseded, rejected, `no_push`, all push-excluded, a PR
  that carries the head, a push branch no push reaches, a recorded landing), gets its review and
  a low feed line, at most `review.unreviewed_per_pass` (default: the project's
  `budget.max_parallel_workers`, 0 off) per pass, for tasks
  updated within `review.unreviewed_max_age_h` (default 72); it runs every 5 minutes (every 100
  seconds after a pass cut short that queued a review, so a backlog drains) and in the
  blocker sweep, reads local refs only, and never queues a second review of one head. Its first pass
  after an upgrade logs how many done code tasks in that window have no landing, push-queue entry
  or superseded record, so a backlog of finished changes shows. A code task still in review
  with no review `review.sweep_after_s` (default 300, 0 off) after its last run gets its review
  from the daemon. The sweep only queues reviews and never closes a task, so a task whose
  continuation already passed review still gets its own review (one review each, no work lost).
  The exception is a task whose change already landed on the push branch under other commits
  (the same changed lines, its `delivery.version_bump` files and changesets aside, commit by
  commit or squashed): the sweep, the daemon's review and the fix of a failed review record it as
  superseded (as `ttp task superseded` does), close it if it is in review, tell the coordinator
  once, and queue no review or fix for it.
  A harness task left in review the same way delivers by its commit in the harness, which has no
  remote. Once the commit its hand-off names is merged into the harness, its dependents may start
  while it stays in review, and the sweep closes it as done with no review queued (or once its
  review passed). Until then it gets one review-only review of its own. Once that review passes,
  it is closed as done too when its worktree branch has nothing left to merge; if the branch still
  has unmerged commits, the coordinator is told once. Closing it answers the chat that asked for
  it, as `ttp task superseded` does when it closes one. A queued task held by a
  dependency left in review names it in its start note ("waits on #<id> (in review <h>h)").
  `review.auto_notes` is added
  to each such review's spec (for example, what to do after a push). When the task's PR already
  carries the reviewed head (as pr-watch last read it, or as the task's own `ttp push --own`
  pushed it), the work is delivered: the review is review only, with no push to
  `delivery.push_branch` and no push-queue approval. So is the review of a change that must not
  reach the push branch: every file its diff since the base changes matches
  `delivery.push_exclude_paths`, its hand-off sets `no_push` (true or the reason), or its spec has
  a line starting `no_push:` (the reason follows). A continuation (a fix of a review's
  findings, a `continue:` task) inherits the nearest ancestor's `no_push` unless it says
  `no_push: false` (or its hand-off sets `no_push` false). Prose is never read as a ban: free text cannot
  tell a ban from a sentence that reports, quotes or conditions one. The review's spec says why, its fix and re-review
  stay review only, and the push queue ignores its approval. Every review is review only while
  `delivery.push_branch` can never be pushed to: it names main or master, which `ttp push` always
  refuses, or the code repo has no git remote (or not the one it names). The config check and
  `ttp doctor` say so. A review the coordinator adds
  for the same work replaces the daemon's while it has not started. A review that fails with
  follow-ups gets one fix task on the reviewed branch and a re-review from the daemon, and the
  tasks waiting on the failed review wait on the re-review instead of being blocked. The re-review
  depends on the fix; a fix that ends with no new commits on the reviewed head blocks it and wakes
  the coordinator, so the same head is never re-reviewed (`review.auto_fix_rounds`, default 1,
  rounds per stack: a failed re-review, which would start a third review round, goes to the
  coordinator as one `review_nofix` event instead of being queued; a failure without fix follow-ups still blocks them, and `upstream:` notes and
  deferred follow-ups stay with the coordinator). Across stacks, once `review.area_fail_cap` (3; 0
  off) reviews of one area failed within 48 h (a lineage linked by `continues`, follow-ups,
  `depends_on` and `Review #N` / `Fix review #N` titles, or overlapping changes to the same main file), the
  daemon queues no more fixes there and raises a tricky coordinator turn (one the turn gates never skip) to re-plan it
  ("repeated review failures in one area", also counted in the daily review).
- A review's result: `done` lets the change proceed (its PR, push or push-queue approval and its
  dependents), `failed` stops it and starts the fix flow above. A review handed off `done` with
  `metrics.verdict` `changes_needed` (or `changes_requested`, `rejected`) counts as failed too, so a
  project may word the rule that way. The rule sits in its own block of `prompts/kind-review.md`,
  between `<!-- ttp:result-rule -->` marker lines the prompt leaves out: a project that rewords it
  keeps its wording through template upgrades without merge conflicts.
- A review judges the diff plus the gate results that already exist for the head it reviews, and
  never starts or waits on a repeat of a test that passed on that head: a further test it wants is a
  finding, and its verdict still goes out. Only the required checks (the suite and any smoke test)
  and the review's verdict hold a landing (`prompts/kind-review.md`, `prompts/kind-code.md`).
- A failed or `changes_needed` review is closed as superseded, keeping its verdict: automatically,
  model-free, once a later re-review in its chain passes; or by hand with
  `ttp task <project> superseded <id> --by-review <passed re-review id>` or
  `ttp task <project> superseded <id> <covering commit> [--ref <ref>]` (the commit must be on the push
  branch or `--ref`). Without evidence it is refused. Superseded reviews leave the open lists and the
  unhandled-failed-review check; `ttp task show` names what superseded them.
- Each tier maps to a model and effort per provider (`providers.<name>.tiers`). The coordinator
  runs at `coordinator.tier` (light) unless `coordinator.model` or `coordinator.effort` is set:
  those pin the coordinator alone. Unset, it takes the model of `coordinator.model_tier`
  (`deep` by default: the strongest model in the provider's tier table, opus for Claude; "" uses
  its own tier's), so the cheaper light-tier model does not move the coordinator, and xhigh effort
  (`coordinator.DEFAULT_EFFORT`), mapped to the provider's nearest supported level. The model names
  one of `core_provider`'s models. Coordinator turns never take a tier's route to another provider.
  Each turn's run records the model and effort it asked for and the model the agent's session
  reported; `ttp status` and the daily review show a `coordinator model:` line with the configured
  route against the last turn's, ending in `MISMATCH` when they differ (a config change shows until
  the next turn), and the review lists the coordinator's spend per day for the last 7 days.
  `ttp upgrade` moves a coordinator still on the old default (no coordinator model or model_tier of
  its own) to the strongest model, and also one holding the model_tier "" an earlier upgrade set for
  a project overriding its light-tier model; any other coordinator model or model_tier is the
  project's own choice and stays. The upgrade says once per project which it did. It cannot tell
  that earlier upgrade's mark from a model_tier "" set by hand on such a project: that one moves
  too, and `ttp config NAME coordinator.model MODEL` pins it back.
  `coordinator.turn_timeouts_s.<provider>.<effort>` sizes the turn after effort is resolved:
  low/medium/high/xhigh/max (or ultra) default to 300/600/900/1200/1500 seconds for Claude,
  and 300/600/1200/1800/2400 for Codex and Cursor; an unknown effort or provider gets 600.
  Above 32 KB of prompt text, each additional 8 KB adds 60 seconds. These allowances give slower
  reasoning turns more time while turns at a lower set effort stay short. The base is the first one
  set: the project's own `turn_timeouts_s` entry for the provider and effort, then a flat
  `coordinator.turn_timeout_s` (0, the default, is unset; a stored 600, the old flat default, also
  counts as unset so older projects get effort sizing), then the built-in table. The prompt
  allowance is added to any base. `coordinator.turn_timeout_ceiling_s` defaults to 3600 and can be
  lowered; it caps every result, and the runtime always enforces a one-hour absolute ceiling. Each run records prompt bytes,
  provider, model, effort and chosen timeout; timeouts also leave a daemon log and diagnostic state.
  Every coordinator turn runs at xhigh effort unless `coordinator.effort` sets one; either is
  mapped to the provider's nearest supported level (its highest for one above it, none for a
  provider that takes no effort setting), and a change applies at the next turn. A tricky or
  blocking turn always runs (the turn gates never skip it). Its triggers, kept in one table
  (`coordinator.effort_triggers`) and recorded per turn in the run's note: a user message (and a
  change of plan in it); a blocked, failed or needs-review task, a failed review, a dead dependency,
  an expired or broken deferral or a stalled review; a task failing `coordinator.repeat_fails_24h`
  (2) times in 24 h; a task's external waits (not its own checks, jobs, push or planned window)
  changing reason, probe (`retry_when`) or owner (the task or ask they name), going on for 24 h, or
  reaching `coordinator.repeat_waits_24h` (8) in 24 h (the last two once per run of waits), where
  a wait on a live `ttp detach` job or an unpaused `ttp lock` resource counts only past
  `coordinator.live_waits_max` (6) waits on the same ones in 24 h or once the job's log stops growing,
  and a wait held elsewhere never counts (a user step or a pause the user set, a peer that is recovering or
  listed in the harness's `peer_recovery.json`, or a machine under a known outage recorded only by other
  projects); a rejected action; free worker slots while every queued task is held by a dependency or a paused
  resource or deferred on purpose (`start_after`, `start_when`), at least one of them held (planned
  deferrals alone are routine); a high or critical event or alert; resource trouble (not waits only); a costly or irreversible step (an ask timing out, a task's budget spent, PR findings
  or a clean PR, a failed push, the budget gate entering or leaving red); an idle wake finding
  blocked tasks or open asks. Lowering rules keep known or routine events from counting as triggers, each
  logged in the turn's note (`lowered`): a needs-review hand-off with no follow-ups, findings or notes
  whose review the daemon queues itself; a high or critical event whose fingerprint is a known case
  (an open issue an earlier turn already saw at this severity or higher, a deploy pending on an owned
  merge before its takeover, an open change in the shared machine ledger that expects it: `ttp machines
  change add ... --expect <fingerprint|down|held>`); and a review's failed hand-off counts once, as
  "failed review". `coordinator.effort_skip_triggers` (none by default) lists trigger labels, as
  recorded in the note, that never count.
  Three daemon events that rarely need a decision are high only when it matters (pagesev.py). A
  done task's `continue:` follow-up no task took up (`continue_unqueued`) is normal and rides
  along with the next turn due anyway, at most an hour later. A task the daemon or the push queue
  blocks (`task_blocked`) is normal; the same task blocked again for the same reason within 6 h is
  counted on the queued event, or queued once to ride along. Both are high for an urgent task:
  priority 1, an `urgent:` line in its spec or a required owner of an open obligation, and a block
  is also high while the task waits on an ask. An overdue machine change
  (`machine_change_overdue`) is normal, high only on a machine tagged `serving`, `live` or `server`
  past its end with no passing until-probe, and each project hears of one change once per episode,
  whether from its own daemon or relayed by another project's. A project's expired changes reach its
  coordinator as one item listing each change's what, undo and age with the choices renew, close or
  undo, which it decides without the user's word; a gone or paused project's go to the machine's
  recovery owner, then its fallback. The user is paged only for a risky change (one that stops,
  disables or pauses a service or recovery, or touches networking, ssh, credentials, mounts, the
  kernel or drivers, reservations, a live serving machine, or an unclassified change on a machine
  other projects share; `machine_ledger.change_risk`) nobody renewed, closed or undid for 24 h.
- Untrusted text in the coordinator's digest (every event: hand-offs, notes from other projects, PR
  and CI text, watcher and tool output; a relayer's context and findings) and in a worker's context
  block (a relayer's context, earlier hand-offs, review findings, tried-ledger lines) is enclosed as
  `⟦<tag> data: <source>⟧ … ⟦/<tag>⟧`, with a tag drawn new each turn (`spans.py`). Brackets inside the
  text are shown as plain ones, so it cannot close its span or open a fake one. The user's verbatim
  words are never enclosed.
- `coordinator.session` sizes the persistent coordinator session, which is always on with no setting
  to turn it off: each coordinator wake (chat, task end, timer) resumes one agent session (Claude
  Code, Codex and Cursor, with per-provider tools and guards in providers.md). An old
  `coordinator.session.mode` value in a project's config is ignored, and `config_set` refuses it. A
  daemon restart or a host reboot resumes the same session id (kept in the project's kv table) and
  never starts a new session. A changed system prompt, charter or action schema resumes the session
  too: the wake runs with the new prompt and schema and leads with a model-free "Changed since your
  last wake" block (a prompt version line and the charter's unified diff against the copy the last ok
  wake saved; on Codex also the system prompt's diff). A new session starts only when there is none,
  the provider changed, the prompt changed on a provider that cannot resume with a changed prompt
  (`Provider.resume_new_prompt`; none shipped), the model or effort changed, a new session's first wake
  did not end ok and did no work (one that did, then hit its dollar cap or timeout, keeps its session:
  the next wake resumes it), its transcript is gone (a host move or a provider-side expiry), or the provider
  rejected a resume. A full context never starts a new session: the session compacts in place and
  keeps its id. The context is the last model call's prompt and answer; the compaction point is the
  session's first prompt (or its size after its last compaction, the larger) plus
  `compact_growth_tokens` (100000), and never so soon that a compaction would free under
  `compact_min_free` (0.3) of the context, judged by the size the last one left; at most the model's
  context window (`context_window_tokens`; 0 reads it from a `[1m]` model id, else no cap) less
  `compact_margin_tokens` (40000), which always forces one.
  `compact_tokens` above 0 (default 0) sets a fixed point instead. The older `rollover_tokens`,
  `rollover_growth_tokens` and `rollover_margin_tokens` names are still read for a project that sets
  no `compact_*` value. A resumed wake that fails is recorded as an event
  (`coord_session_resume_failed`) and its batch reruns at once in the same session, so each message
  and event is handled once. Only when the provider rejects the session id (no such session, or it
  expired), or 3 resumes in a row fail without a model call, does the batch rerun as the next
  session, seeded with the hand-off. 3 failed resumes within an hour also raise an issue, and nothing
  pauses the session. A new session that cannot start is recorded (`coord_session_start_failed`) and
  retried after the usual backoff, again as a seeded session. There is no session spend guard: the
  project and account dollar caps and the per-task budgets are the only limits on its spend. A
  coordinator on a provider that runs no session (the chat provider, for now) keeps stateless turns,
  with no session line in `ttp status`. `ttp status --verbose` shows the session's
  short id, wakes, context size and last decision. `ttp stats <name> --coord-session` reports each
  day's wakes, $/wake, context growth and tool calls per wake, in-place compactions (session id,
  tokens before and after, how) and cache-read share beside
  the stateless baseline (the 7 days before the first session wake: turns/day, $/turn), and flags
  the break-even (at most 2 tool calls and 20k context growth per wake) and a day costing more
  than the baseline's $/day. Each day counts wakes resumed, new, new with a hand-off (new for any
  reason but the first session) and stateless (expected 0; older history rows keep their own labels,
  such as handover, rollover, spend guard or pause), names why wakes did not resume (a new session and
  its cause, a rejected resume, a provider that keeps no session, decide failed), counts compactions
  in place, and lists failed session wakes with their transcript's error. In session mode the system prompt is the
  Restrictions, the coordinator role with `prompts/coordinator-session.md` and the charter verbatim:
  the same bytes every wake while the charter is unchanged, so the prompt cache holds. Every
  session wake sets the agent's own compaction switch at the compaction point (Claude Code and the
  chat provider: `CLAUDE_CODE_AUTO_COMPACT_WINDOW`; Codex: `model_auto_compact_token_limit`), so it
  compacts its history inside the same session. Only where the agent has no such switch (Cursor),
  the wake at the compaction point also asks for a `session_note` (at most 4,000 tokens: open
  threads, promises in their chat wording, pending checks, decisions with their reasons, user
  corrections), saved as `state/coord-session/<session id>.md`; it is the harness's summary of the
  same session. Each compaction is recorded, and the next wake starts with `# CONTEXT COMPACTED`:
  the standing memory, the open order ledger, the session's latest note, the last 8 wakes' own
  summaries and the last 20 chat exchanges verbatim, restored from disk, then the full digest. A
  new session (only for the causes above, never a restart or a full context) leads with the same
  parts as its hand-off, also written to `state/coord-session/<previous session id>.handoff.md`; a
  part that cannot be read is named, never left out silently. The compacted wake's lead says the
  agent's summary is the session's own notes, never the user's word. An injected instruction cannot
  ride across wakes as a user order: a session note line labelled as the user's words ("user's
  word(s)", the owner's too, "User (msg #N):", "per the user/owner", "the user/owner said|wrote"
  right before a quote) is kept only when it cites a logged message from the user (msg #N) whose own
  words hold each of its quotes, the check charter changes use (the owner's agent's words count too
  with `coordinator.agent_orders_as_user`); a failing or quote-less labelled line is dropped and
  logged as an `order_claim_dropped` event, when it is saved and again when a seed or restore
  carries it. Unlabelled lines (a paraphrase citing a msg, a quoted term) are carried as the note's
  data; put names and terms in backticks. The note's header vouches only for that drop: nothing in
  it is the user's word unless the chat log, order ledger, memory or charter shows it. A
  `memory_add` with a labelled line that fails the same check is refused; a paraphrase is stored as
  data. A coordinator run's `ttp memory` writes are refused (use `memory_add`); the user's own `ttp
  memory` is unchanged. Known gaps: only quotes are checked, so unquoted text on a labelled line
  with a checked quote is carried; a claim phrased past those label forms ("the user ordered
  '...'", "as the user put it", "User:", another language) is not checked and is carried as data
  under the header above. A session wake's per-wake dollar cap, with `coordinator.session.wake_budget_usd` unset, is sized to
  the session: twice the dearest of the last 20 resumed wakes that ended ok, never below the sized turn
  budget (`coordinator.turn_budget_usd`) nor past what the project and account dollar caps leave; a set
  value wins. A new session's first wake's dollar cap is the per-wake cap plus the expected seed cost,
  within the project and account dollar caps only, never the hourly coordinator limit (seedcap.py): the dearest of the last 3 new-session wakes' seeds (their
  cache writes and uncached input at their own booked rate), each counted at most 3 times its
  seeded prompt size (`turn.session.seed_chars`; older wakes without it, their size before the
  seed) / 4 in tokens. The factor 3 rests on one measured wake, which wrote 2.6 times its size
  before the seed; its ratio to the seeded size is lower and was not measured. A resumed wake sends only the digest sections
  that changed since the session's last wake that ended ok, each whole, plus the new events and one
  line naming the sections left out. Only the Open tasks section, keyed by task id, and the Course
  check, keyed by sign and task id (`- costly: #<id>`), go row by row (`row_delta`, default on; `false` sends every changed section whole): the added and changed rows,
  each whole, and a line naming every added, changed and removed task and counting the rest; an age or
  read time alone is no change. It still goes whole when a task id repeats, its list is cut short by
  its row limit, its heading changed in more than its counts, more than half its rows changed or its
  rows only changed order. Every other section (memory, user corrections, promises, chats,
  resources and the like) is compared as raw text and sent whole on any change, so one changed
  line there resends the section. Accepted gap: a chats or promises section whose only change is an
  age is resent whole too.
  A new session, a daemon restart (the digest then goes whole into the same resumed session), a
  failed or compacted wake since, or the action `{"type": "noop", "key": "full_digest"}` sends the whole
  digest on the next wake. A session wake's answer is checked before its turn ends: an action the
  daemon would refuse (an unknown task id, a dependency that failed, a task cap reached, a field a
  running or finished task keeps) comes back as the answer tool's result, so the coordinator fixes
  or drops it in the same turn. An action resent unchanged goes through and is refused after the
  turn as before; at most 2 answers a wake are sent back.
  `tools` (`full`, the default, or `read_only`) sets what a session wake may use (coordtools.py).
  The session prompt's Tools section names only what the wake really gets on its provider
  (`Provider.coord_tool_set`): a worker's tools, read-only tools or none, with the reason, and hands
  what it cannot do to workers.
  With `full` it gets a worker's tools (on Claude Code its whole standard set, subagents
  among them, and the web tools unless `no_internet`; its text is appended to Claude Code's own
  system prompt) under a worker's PreToolUse checks, so it can edit, build, ssh to the
  machines its charter allows and run the `ttp` CLI. Direct writes to the charter, the memory
  files, `harness/project.json` and the state database are denied, each naming the action or
  command that applies the same checks (`charter_update`, `memory_add`, `config_set`, `ttp config`).
  As those checks read the command text, each Bash call is also compared before and after: a change
  to the charter, a memory file or `project.json` that `ttp` itself did not write (a variable, glob or
  script write) is put back and reported in the tool result and as a `coord_protected_write` event.
  `ttp` logs each of its own writes to these files (its file writes, and every git command that changes
  the harness checkout: a harness task's landing merge, `ttp upgrade`'s merge and its put-backs) in
  `state/protected-writes.jsonl`, so a `ttp config` or `memory_add` inside the call, a landing or the
  daemon writing meanwhile is kept. Before a put-back the changed content is saved under
  `state/coord-put-back/` (indexed in `state/coord-put-back.jsonl` with its path, digest, time and
  command) and the notice names it. A guarded file outside a project's harness folder has no write log:
  its change is only reported, never put back. Known gap: your own editor edit to the charter, memory
  or `project.json` while a coordinator Bash call runs is put back too (saved and reported), as the
  guard cannot tell it from the coordinator's. Known gap (write/log race): `ttp` logs a file write
  only after the file is replaced and its folder synced, and a git command's changes only after the
  command ends, so a guard check inside that window takes `ttp`'s own write as unlogged and puts it
  back (saved and reported); a `ttp` write that lands between the guard's read and its put-back is
  overwritten without being saved. Known gap (restore-to-snapshot): the put-back restores the file
  as it was before the call, so when one call holds a logged `ttp` write and later an unlogged edit
  to the same file, the put-back undoes the `ttp` write too (the combined content is saved).
  The state database is not compared (the daemon writes it all the time); only the command-text
  check covers it. Any raw `git push` and any `ttp push` that lands or moves a tag is denied, by the hook and again
  at run time by `ttp push` and the harness's `git` wrapper: code goes through a work branch, a
  worker task, review and the push queue, and a channel tag moves only in a release cut task. A
  coordinator run holds no device (`TTP_NO_DEVICE=1`), so the worker device-command refusals bind
  it: a reset, flash or power tool, a device node path or a change to the device mask outside a
  device `ttp lock` or `ttp devq` is denied, since heavy device work goes to a worker task. A
  foreground command stops at `tool_timeout_s` (300, at most 600); a longer or background one is
  denied with a pointer to `ttp detach` or a worker task. Each mutating call (the command or file,
  its exit code) goes to the run's `tool-audit.jsonl` and `progress.md`, which the web run view
  shows. Before a fix the coordinator runs `ttp audit why "<why>" --serves <o12|msg123|task456>
  --undo "<how>"`; the examples show the shell-safe forms (an unquoted `#` would start a comment in
  bash and drop the argument): each later mutating call in that wake carries the why, the order or
  task it serves and the undo (coordfix.py). A call with none is flagged once in the next digest, the daily review lists
  the day's fixes grouped by order, and `ttp audit fixes [--since 6h]` prints them. `read_only` keeps `Read` and a list of read-only `ttp` commands. Moving to `read_only`
  needs no word; back to `full` needs the user's. A stateless turn has no tools either way.
- Rejected-action cooldown: when the daemon rejects a turn's action (not an information-only
  refusal), each event that woke that turn puts its key (event kind, source and task) in a cooldown
  of `coordinator.rejection_cooldown_s` (default 900; 0 turns it off). Inside it, an event with the
  same key, the same text (counters, ids and times aside) and no higher severity is counted and
  muted, not woken, and the next turn's digest shows one line per key: `cooldown: <key>, N events
  since rejection, rejected because <reason>`. Changed text, a higher severity or a user message
  ends it at once and wakes; when it ends with events counted, one summary event wakes one turn (a
  turn that rejects again on it restarts the cooldown). Never cooled: user messages, critical
  events, answered asks, and the safety class (spend or budget caps, restrictions, data loss, device
  or machine holds). The state is kept in the project's database, so it survives a restart.
- Blocker sweep: every `coordinator.blocker_sweep_h` (6; 0 turns it off) hours while anything is
  stuck, and sooner (never within `coordinator.blocker_sweep_min_gap_h`, 1) when a pause ends, an
  ask is resolved, a dependency finishes, a provider pause ends or a re-check comes due, a
  tricky turn lists every blocked, waiting, in-review, setting-held and deferred task and every
  paused resource with its age, reason and last re-check. The coordinator records one `sweep`
  outcome for each; items without one, `still_blocked` ones with no re-checkable condition, and
  re-block loops (the same reason twice, or a spec addendum while still blocked) are flagged in the
  next sweep. Reviews older than `coordinator.review_backlog_h` (6) get a line on why their review
  has not run. A hold no turn can lift needs no outcome and is listed as no action: a user pause (a
  probe that only tests for a marker file named `user-*`; `coordinator.user_pause_marker` sets the
  pattern), a resource pause the user set, or a deferral only until a time. A `human_indispensable`
  outcome on a task a user-set resource pause holds covers every task naming that resource until
  the pause row changes, a user message arrives, or another project's note relays the user's words
  (a non-empty `user's words (verbatim)` part); then the sweep judges them again. A blocked task or
  coordinator-set pause whose hold and `human_indispensable` (or probe-less `still_blocked`) outcome
  name one open ask is listed as `HELD BY OPEN ASK <id>` and starts no sweep, not even on a budget
  kind change, until the ask is answered or withdrawn, a user message arrives, the item changes, the
  ask passes `coordinator.ask_rethink_h`, or a day passes since that outcome. An outcome stored
  in an older format is no cover. A waiting task on a user-pause probe is never reported
  as a stale wait. A sweep whose items are all unchanged since the last model sweep (within 24 h),
  each with a model-free end, is skipped model-free with a receipt naming each item's end. A re-check
  an outcome asked for that comes due skips the same way only within
  `coordinator.blocker_sweep_recheck_skip_h` (6) of the last model sweep and with each item's updated
  time and probe answer as at the last sweep; the receipt carries it on until a model sweep runs. A pause
  the coordinator set counts when its `end_when` probe answers (not broken), a heal check's health
  end lifts it, or, with neither, its `until` falls before the next sweep is due. The daily review lists everything stuck over 6 h with its sweep outcome.
- Course check: a digest section, shown only when something trips, with model-free signs the project
  is off track or wasteful, each with its evidence and a suggested correction: an open task that
  failed `fails_24h` (3) times in 24 h; queued tasks that never ran held `queued_h` (6) hours on a
  dependency or paused resource while worker slots are free; a task that spent `spend_ratio` (3)
  times the 7-day median of its kind and at least `spend_min_usd` ($2), given `spend_min_samples` (5)
  finished ones (at most 10 lines, open tasks first, then a count line; `ttp course <project>` lists
  them all); and the last `idle_turns` (6) coordinator turns in a row that decided nothing.
  Thresholds live under `coordinator.course_check` (0 turns one off). The coordinator corrects course
  in that turn or says why it is fine.
- Corrections: a user or chat message that points out a miss is a likely correction, flagged
  model-free when it matches a short generic phrase list ("why didn't", "keeps failing", "stuck for",
  "you missed", "nobody noticed", ...) or arrives while a stall event or a Course check line is
  open. The digest tags it; the coordinator fixes the case
  and adds a `harness` task for the class (the detector or default that would have caught it). A turn
  that added neither that nor an `upstream:` task, and gave no `correction #<id>: <why>` in its
  summary, gets one Course check line in the next digest. The daily review's efficiency audit lists
  each correction of the last 24 h with its class fix task, or none.
- Retired-rule echoes: when a `charter_update` replaces text (`quote`, `replaces`), a `memory_add`
  `supersedes` entries (or `ttp memory --supersedes`) or a user Correction names a wrong value in
  prose, the daemon keeps the retired text's distinctive phrases (quoted and backticked spans, config
  keys and identifiers, its first sentence; a phrase the new text keeps is left out, and so is plugin
  vocabulary such as `start_when` or `ttp list`) for 30 days. Each
  coordinator turn it searches, model-free and case-insensitively, live memory entries, charter
  sections, schedules (command, precheck, heal check, prompt, description), open tasks' specs and
  `start_when` probes, config values and the project's own files (harness scripts, tools, hooks and
  prompts; `.claude/skills`, `.claude/hooks`, `.agents/skills`, `CLAUDE.md`, `AGENTS.md`; and the files
  or folders in `coordinator.echo_paths`, such as a repo's recovery code), and lists the hits in a `Retired-rule echoes` digest section
  (place, matching line, which retirement; at most 10 lines; a hit still there a turn later is marked
  `still here`; a last line names every memory entry that hit, for memory_forget). The replacing text, the archives and harness files equal to the template are not searched; a Correction's phrase right
  after a negation ("never ...") is no hit. The coordinator fixes each in that turn (memory_forget or
  `supersedes`, charter_update `quote`, schedule_set, task_update, config_set). It is a notice: no
  event, wake, hold or model call of its own. An audit record that only preserves an old assertion is
  declared historical in `harness/echo_historical.json` (its path, sha256 and the retirement's id): while
  its bytes still match, that hit is counted, not listed; any mismatch lists it again. A task whose spec quotes the old rule to remove it is
  listed too. Only a rule the user withdrew (a charter `quote` or `replaces`, a Correction,
  a memory change whose `why` cites the user's words, or one on the user's turn that cites or quotes
  their new words or whose message names the old rule, including a `memory_forget`; a dedupe on the
  user's turn is not one)
  gives a fix order, and only where its governing words match as a whole: a compacted fact, a coordinator
  decision superseded by another, a retirement of unknown origin, or a match on a file name, path, version or
  evidence reference alone (or a name the retired text holds only inside one) is advisory, counted on one line. `ttp echoes [--json]` lists, read-only, every retirement (kind, text,
  replacement, source, time) and every match with its class, including those the digest leaves out. A match that is no echo (code using a name or field the retired text also holds)
  can be acknowledged: `ttp echoes --ack <retirement> <place>` (the retirement's id and the place as the listing names it, a file's `<path>:<line>` or a label such as a task spec).
  The acknowledgement is stored in the project's kv store (`retired_echo_acks`) with a hash of the matched line; while that line's text is unchanged the match is listed as
  `acknowledged` and gets no fix order. A changed line, or a new copy of it in the same place, lists the match again.
- Progress and stall check: each minute the daemon checks, without a model, that work keeps yielding
  results (a task done, a worker hand-off, a driver's `result` report). It judges only work that has
  run. A `no_progress` event names a task with a worker run running, a resource running worker runs
  hold, or the whole project, once it has gone `task_h`, `resource_h` or `project_h` (2 each) hours
  with no result since the run started (the earliest such run for a resource or the project). A
  queued task, never started or queued again after a run, is not judged here: the daemon's stall
  check (`coordinator.stall_wake_h`) owns it, since it knows whether dispatch would start the task
  now; its stall observation also lists each ready task dispatch holds only for a busy slot or a
  taken `ttp lock`, with the hold dispatch recorded (the running tasks filling the slots, or each lock
  holder with its pid, named as a live holder only while that pid lives). A paused task, paused
  directly or through a `device.locks` name, is never listed as a lock wait. Dispatch also logs once
  per wait when it skips a ready task for a busy resource. Known gap: that check wakes only when no
  worker run at all has started for that long, so one ready task that dispatch passes over while
  other runs keep starting is not reported. A `change_probe` event names `change-probe:<id>`: an open
  machine change of this project whose until-probe passed `change_probe_m` (15) minutes ago while the
  task it names is still queued or waiting; it clears when the change closes or the task wakes. A
  `stall` event
  names a repeat: a retry_when or start_when probe still saying not yet after `probe_n` (10)
  verdicts and `probe_h` (1) hour (waits
  on the task's own job, a push, CI, another task, a time or the user's step (wait_kind `user`, or an
  obligation owner whose wait names the user) are left out), a task's or schedule's
  worker runs failing `fail_n` (3) times in a row the same way, or a schedule failing `repeat_n` (5)
  times in a row the same way (a skip on its own daily cap is the schedule-budget observation's, not
  counted again). A driver's refusals and failures are counted once, by the `ttp progress report`
  streak check (`waiting.progress.stall_after`). A probe that waits on a down or paused resource the
  task itself is meant to fix (or whose holder waits on the task) is flagged "waits on what it fixes".
  A probe whose ssh host is confirmed down is re-probed at 15, 30, 60, then every 120 s (not
  every few minutes), so a worker writes no poll loop for it. For a machines.json alias the ssh
  target names, the daemon also acts once per down episode: a `recovery_owner` that is another
  project gets one high note and nothing runs here; a pause on the machine or the fix's resource,
  its lock held or an open machine change runs nothing (logged); else this project's heal check
  with a fix runs at once (kicked again hourly while down); with none, one `host_down` event. See
  the tt-project-harness self-healing reference.
  Each key wakes the coordinator once, again when its count or stuck time reaches `escalate_x` (2)
  times what it was at the last wake or after `rewake_h` (12) hours. A key that drops out of one
  check (between two runs) is not cleared and never wakes again as new: it clears only
  after `clear_n` (3) healthy checks in a row spanning `clear_min` (30) minutes (schedule repeats
  also lapse after `quiet_h`, 6, quiet hours). The digest's `Progress` section lists the open keys
  and the daily review lists each with how long it took to clear. Thresholds live under
  `coordinator.progress_check` (0 turns one off); a mute on source `daemon:progress` matching a key
  keeps it quiet. A driver's `result` report (`ttp progress report result --key <key> ...`) resets the
  clock of its key, of `task:<id>` or `resource:<name>` when that is its key, and of the task whose
  run made it. Known gap: a `ttp detach` driver that died without an .rc, or ended while the task's
  probe still says not yet, shows as died or ended in its detach record (below) but raises no progress
  event; the task's own probe and the probe-repeat `stall` key are what surface it. Log growth is never
  treated as progress.
- Promises: a coordinator reply or notify that commits to a deliverable or a time ("I'll report the
  sha by 15:00", "ETA 18:30") is always recorded as a promise. It holds the chat, the promised
  words, the due time (the time it names, in the home zone, else `coordinator.promise_default_h`, 6,
  later; a named date already past or over 14 days ahead is ignored and its clock time applies), the batch's task
  that replies to that chat, and any obligation the same batch records or attaches that such a task
  owns. The digest lists open promises and marks overdue ones; the blocker sweep lists an overdue
  one as an item. Once overdue, one tricky wake asks for the deliverable or an honest update in
  that chat. A promise closes when its tasks finish and a reply reaches its chat, when every linked
  obligation has its receipt or is dropped, or when the
  coordinator closes it (`obligation` with its key, or a sweep outcome `closed` or `retired`).
  A reply whose `delivers` names an obligation or promise (`o1`) or its owner task (`#12`) marks it
  delivered; it closes once every owner task finished. A promise is delivered only in its own chat.
  Large sweeps split into durable consecutive batches, one per turn, with disjoint item coverage.
  `coordinator.sweep_prompt_chars` bounds the sweep section (12000 characters by default, clamped
  to 2048–16000); reasons and diagnoses are clipped to 200 characters each. This bounds added sweep
  context while preserving every identity and outcome request, including items beyond the old
  60-item cutoff. A restart or failed turn retries its batch; remaining batches stay queued.
- Asks never block: every ask stores its recommendation as its default and shows when it applies
  ("acts on X at HH:MM ZONE unless answered") in the chat, `ttp status`, the web app and the
  digest. Unanswered after `coordinator.ask_timeout_h` (0.5; 0 turns it off; settable from the
  chat), the daemon resolves it by applying the recommendation. It tells the chat at low, records
  a decision memory, raises one `ask_timeout` event per tick and refuses a re-ask; tasks anchored on
  the ask are released. Asks only the user can answer stay open (review, merge, another human's
  request, a recommendation that acts as the user, a credential or money grant, an irreversible
  step, a restriction ask whose recommendation would loosen it: only the user's own words loosen a
  restriction). An open ask never blocks other work; it holds only the tasks anchored on it. The same
  question while it is open, also in other words, is refused naming it (review and merge asks: same
  text only), unless `not_covered` says what is new. For an
  irreversible or restriction ask the timer reads no prose for meaning: only a recommendation that
  is wholly one bare decline (no, decline, skip, keep it, leave it, do nothing, no change) applies;
  any longer wording, even a decline, leaves the ask open for the user. A re-ask of an answered
  question is refused also when one of its question clauses repeats one of the earlier ask's in
  other words; `not_covered` saying what is new still lets it through. Accepted gap: an as-user action worded outside the timer's
  word list in another kind is left to the ask gate and the coordinator's classification. Nothing
  resolves at a red gate or after the user wrote since the ask (the coordinator checks instead). A model-free check raises an open ask with no default, or one
  past its timeout and still open, once per change.
- Ask rethink: an open `restriction`, `irreversible` or `human` ask that holds a blocked task
  longer than `coordinator.ask_rethink_h` (2; 0 turns it off) raises one `ask_rethink` event, a
  tricky turn that decides the ask itself unless only the user can (then `human_indispensable`
  with a why). Each ask is raised once, across restarts; review, merge, access, funds and spend asks
  never are. A `still_blocked` on a task such an ask holds is flagged, not carried.
- Hold loops (model-free, every tick): work nothing will move raises one event while it lasts and
  clears once resolved. `time_only_hold`: held only until a time (`until:` or `start_after`) with
  no open dependency, ask, resource or probe. An `until:` hold is raised once its review passed,
  its time was re-armed or it stayed so past `coordinator.hold_max_h`; a queued `start_after` is a
  planned deferral, raised only once its time was re-armed or, an hour after that time passed, it
  never started since it was deferred and nothing else (a retry backoff, a resource hold, running
  work) holds it; that overdue flag clears once it starts. Re-arming the time does not clear it.
  `blocked_dependency`: a queued or blocked task depends on a blocked task with no end of its own.
  `dead_dependency`: a blocked task depends on a failed, cancelled or missing one (both after a
  coordinator turn, which a re-hold no longer postpones). `dependency_cycle`: tasks that depend on
  each other, named; `task_add` and `task_update` refuse to create one. All are listed in the
  `stale_holds` event and flagged as loops in each blocker sweep.
- Premise commands (model-free): a code task's spec line `premise: <words>` is checked once by a
  light question before its first run (an urgent task checks it itself). When a read-only shell
  command can answer it, add `premise_cmd: <command>`: the daemon runs it itself from the project
  root, devices masked, before each dispatch, and again before a later dispatch once its pass is
  older than `premise.recheck_min` (30, retries included). Exit 0: the premise holds and the task
  starts. Exit 1: the premise is false; the task closes as not needed with the command, exit code
  and output tail in its result, no worker runs, the coordinator gets one `task_done` event, and
  `task_add` refuses the same command for 14 days. Any other exit, a run past
  `premise.cmd_timeout_s` (60) or a command the read-only guard refuses (writes, git that changes
  refs, ssh, schedulers, device tools) falls back to the light question, never a silent skip. Map
  other scripts onto 0 and 1 with a wrapper, e.g. a fix already merged:
  `premise_cmd: git merge-base --is-ancestor <sha> origin/<branch> && exit 1 || exit 0`. The daily
  review counts premise-closed tasks and the cost they saved at the tier's median code run.
- Dead landing waits (model-free): a queued task whose start_when is exactly `landed:#<id>`, with
  that producer done, not landed and nothing open that can land it (no review, fix, rebase, task on
  its branch or push queue row) for `waiting.landed_wait_dead_s` (900), raises one
  `landed_wait_dead` event naming the held tasks, judged from saved task rows only, so a restart
  neither clears nor repeats it. A `landed:#<id>` inside a longer probe is not judged; the blocker
  sweep covers it.
- Absent outside waits (model-free, absentwait.py): a waiting task's retry_when or a queued task's
  start_when that queries something outside (curl, wget, a URL, `gh api`, a package index) and
  gives the same absent answer (HTTP 404, not found, not listed, withdrawn) `waiting.absent_close_n`
  (3; 0: off) times in a row, or whose hand-off already cites settled evidence (repeated 404s, "not
  listed") once it has said not yet, is closed now: done with outcome `unavailable`, one `task_done`
  event, not held to its deadline. Credential, funds and quota-reset waits (budget, spend, rate
  limit, a daily reset, login), time-only probes, the task's own job and reply waits, busy locks,
  unreachable hosts and code and review tasks are exempt. Each close registers one reopen watch:
  the same probe every 6 h for 14 days, off the tick; its first pass queues one `reopens:<id>`
  follow-up with the closed task's spec.
- Expected finish of outside waits (eta.py, `ttp eta`): a waiting task whose `retry_when` watches work
  outside the harness (a Slurm job, a detached run, another project's ack: any probe but a bare clock
  gate or `landed:#<id>`) shows 'no measured finish yet' with no time, in `ttp eta`, the coordinator's
  Expected finish lines, its dependants' critical path, obligation ETAs and the long-promise course
  check; its next probe is never taken for its finish. Its hand-off may give `eta` (epoch seconds, or
  an ISO time with UTC offset) with `eta_basis` (the measurement behind it, such as the job's own
  progress rate): its finish is then that eta, plus the median run unless an `on_pass_cmd` ends the
  task. A detached driver of that task updates it between wakes with `ttp progress report <kind>
  --eta <time> --eta-basis "<how measured>"`: the newest valid eta from the hand-off or the task's
  reports since it began waiting wins. An `eta` without `eta_basis`, already past or with no offset is
  ignored. Waits on a time and on `landed:#<id>` keep their estimates, and so does a wait the daemon
  already woke (it waits only for a slot, lock or gate).
- Task-to-task waits (model-free, lanewait.py): a queued task whose start_when is exactly
  `landed:#<id>` starts as soon as that change is approved or batched in the push queue, branching
  from its approved head instead of the base, when the task gets a worktree of its own (code, or a
  kind `worktree.kinds` lists; no carried branch or continued head). Any other task waits for the
  real landing, as it would run on code without that change. If the batch later sends it back, the task gets a
  rebase note (its spec and a running run's steer.md), never a cancel. A queued code or work task
  that has waited on another task (`landed:#`, depends_on, a lineage hold) longer than
  `waiting.lane_wait_event_s` (600; 0: never) raises one `lane_wait` event naming both tasks, the
  holder's status and the hours so far; it clears once the task starts. Waits on anything else (a
  secret, a sign-in, a cadence) raise none.
- Spec-wake hold (model-free): after a spec wake (an urgent task's spec change or a `wake_now`)
  whose run hands back the same wait (`waiting_for`, `retry_when`, branch head), later spec wakes
  of that task sleep on its `retry_when` for `waiting.spec_wake_hold_s` (7200, 2 hours; 0: off) or
  until its own retry time, and its row says until when. Its probe, its timer, a changed wait and
  every other wake still wake it. Urgent spec wakes within 5 minutes of the last one share one wake.
- Provider failover (model-free): a queued, deferred or waiting task whose provider cannot start
  new work (logged out, a limit pause, or a red gate such as a plan window at the line) for longer
  than `coordinator.provider_failover_after_h` (4; 0 turns it off) moves to the first usable
  provider in `coordinator.provider_failover_to` (empty) or else the core provider. A pin to a
  provider other than the core one is a preference unless the task's `pin_required` is set (only
  when the user's own words require that provider; label `pin:required`, kept by its reviews, fixes
  and follow-ups): a preference moves at once, to the core provider first, whenever its provider's
  gate holds it. Its model pin is cleared, its effort and budget kept (the nearest effort the new
  provider takes), its tier set from the effort (e.g. Codex `xhigh` to `deep`), and a spec note says
  what moved, why and when the old provider resets. A required pin stays; running tasks are only
  reported. Each move is a feed event, one low alert and a line in the daily review.
  No task moves to a provider outside `providers_allowed` (unset: every configured provider), and
  tier routing and task pins stay inside it too (skills/tt-project-harness/references/providers-allowed.md).
- Stall check (model-free): when no worker run has started for `coordinator.stall_wake_h` (3; 0
  turns it off) while a queued task has been one dispatch would start now for that long, one high
  observation lists those tasks and when the last worker run started. It uses dispatch's own checks,
  so tasks held by a dependency, a timer or probe, a paused resource, a busy lock, a logged-out or
  offline provider, or the budget gate's slots are never counted: each hold has its own alert.
  Known gap: a hold that is itself wrong (a lock that never clears, a probe that can never pass) is
  not reported by this check; the lock, hold and wait checks cover it.
- Pick order: on every dispatch pass, read afresh from the task records (nothing carries over a daemon
  restart), dispatch tries the required owners of an open obligation first (earliest due milestone
  first), then priority 1 first, then the oldest. Gates, locks, deferrals and `max_parallel` still
  apply, and no running task is stopped. A task waiting on its own detached job holds no worker
  slot: only live model runs count against `max_parallel`.
- Review hop priority: a daemon-queued `Review #N`, `Fix review #N` or `Re-review #N` takes the most urgent
  priority of the task it covers and its chain's root, and follows when task_update changes theirs, unless
  its own priority was set with task_update.
- Urgent slot wait (model-free): an obligation owner that dispatch holds only because every worker
  slot is taken, for `coordinator.urgent_slot_wait_min` (10; 0 turns it off), raises one high
  observation naming it and the running tasks holding the slots with their priorities. An urgent
  task (below) raises one once it has waited that long since it was queued and anything holds it
  (slots, the spend or auth gate, a pause, a lock, a branch), naming the hold. One per task until it
  runs again. A user-ordered urgent task (a fresh `urgent:` line) that names a machine or device
  resource gets one sooner, after `coordinator.urgent_start_wait_min` (5; 0 turns it off): still
  queued, naming what holds it, or running but waiting that long in `ttp lock`, naming the lock's
  holders (project, task, run).
- Ready to resume goes first: within one priority, dispatch picks a task whose wait is over (its
  `retry_when` passed, or its checks, job or on_pass_cmd woke it) before any other queued task, the
  one ready longest first; priority still wins across levels. A passed wait whose on_pass or
  on_pass_cmd settles it is recorded by the daemon itself, model-free, with no worker slot. One that
  is still not started `coordinator.resume_wait_min` (15; 0 turns it off) after it became ready
  raises one `resume_wait` observation naming it and what holds it.
- Announced, not started (model-free, observation only): each open promise, each task announced
  in a reply (`reply_chat`) and each `ready:` follow-up (below) that no owner task has started a
  run for within 10 min (5 min when an owner has priority 1) raises one `announced_not_started`
  event naming it, its owner (`none` when it has none), its age and what parks it (a `start_after`, a dependency not started, nothing). An owner
  held by a recorded external condition (an open ask naming it, a review, a `start_when` or
  `retry_when` probe that is not only a time, a paused resource, a dependency that is running or
  waiting) raises nothing. It clears once an owner starts or the promise closes; one per promise or
  task, and only for those under a day old. It holds no dispatch.
- Ready, not started (model-free): a queued task with no unmet dependency, deferral, pause or
  resource hold, while a worker slot of its provider is free, that has not started 10 min after it
  became ready (2 min at priority 1) raises one `ready_not_started` event naming what holds it. Full
  slots, pauses, locks, device holds and login or network holds are left to their own events and
  `ttp throughput`; an urgent wait the check above already raised is not raised twice. A woken wait
  whose own `ttp checks` or `ttp push` run is still live (running, or queued for a host checks slot)
  is held by that run; once past `waiting.max_hold_s` such waits fold into one queued
  `checks_backlog` line (count, oldest wait, who holds the host checks slots), never one `wait_stale`
  per task; that line rides along with the next coordinator turn (at the latest an hour after its
  first fold) and starts none by itself. A run that is dead or lost with no result still pages.
  Dispatch itself is model-free: each daemon tick starts every ready task a free slot can take,
  never after a coordinator turn. Both events are counted observations that ride with the next
  coordinator turn; neither wakes it on its own.
- Deferral rule: work whose inputs are ready starts now in its own task without `start_after`; defer
  only for a named external fact (a time another party sets, data that must accumulate, a person, a
  shared resource). A `task_add` with `start_after` whose `why` names none (a `start_when` probe is
  itself the named fact: the daemon checks it live), or a `task_update` adding spec to a task
  deferred more than 10 min, gets this as an action note, inside the task's own note when the action
  already has one. It never refuses; the count is kept in `parked_work_nudges`.
- Ready steps of a waiting worker: a worker does every independent step of its spec before it hands
  off `waiting`, and lists each step it cannot finish whose inputs are ready as a follow-up titled
  `ready: ...`. The coordinator starts each one at once in its own task, never folded into a task
  waiting on CI, a soak, a device or another task. The announced-not-started watch counts a `ready:`
  follow-up as announced work from when its run ended: its owner is a later task titled as the step
  (with or without `ready:`) or whose spec quotes it, `none` until one exists.
- Idle capacity (model-free): a device resource of the project (its device lock, a `*-device` name its
  config or recent tasks use, or a machine tagged `device` that its charter's Resources names) that
  no open task, `ttp lock` slot, reservation or detached-job hold uses for `coordinator.idle_capacity_s`
  (1500; 0 turns it off), while a worker slot is free, the gate is green or yellow and dispatch would
  start nothing, raises one `idle_capacity` event naming it, the running work and the next scheduled
  check. Paused resources, resources in trouble, machines with an open ledger condition or an open
  change entry that holds their device work (a held, paused or drained broker or queue, said as a
  phrase; file names and recovery switches such as auto reset do not count), and devices
  no open task names and no task run in the last `coordinator.idle_capacity_use_days` (7; 0: no
  filter) named never count. One event per idle stretch; one still idle is raised again only after
  `coordinator.idle_capacity_rearm_s` (6 h), doubling each time up to 48 h. When the coordinator
  handles an event without a task naming the device, that decline is kept per device in the
  database: its next idle stretch waits both the grace and the doubling rearm from its last event,
  until a task names the device again.
- gh logged out (model-free): one check per machine, shared by every project on it, runs `gh auth
  status` at most every 15 min while a project there uses gh (a PR to watch, or a schedule or
  probe that calls gh or `ttp ci`). While it fails for the login, one normal alert in the top
  section names the machine's ssh alias and the step only the user can take (`gh auth login`
  there). It is sent once per outage, the other projects there show it quietly, and it clears once
  the check passes. Shipped gh callers never read a logout as ok: pr-watch's run ends with an
  `error:` status, `ttp ci` exits 4 with a `broken:` line, and a gh call that failed its login asks
  for a recheck at once. A `start_when` probe that fails for gh's login (exit 4, or `not logged in`
  in its output) raises no broken-probe event of its own while the host check says logged out; the
  one alert covers it. A command watcher or schedule of your own that calls gh should do the same:
  print `{"error": true, ...}` on a gh auth failure, never an ok line.
- Network outage (model-free): when every task that could start waits on a probe saying this
  machine cannot reach its host (ssh exit 255, confirmed by `ssh <alias> true` failing too; a 255
  that names no host, or whose host answers, is a broken probe for its task), no worker runs and no other task is ready to start, for
  `waiting.unreachable_alert_s` (900; 0 turns it off), one normal alert asks the user to check the
  network connection of the machine running the project, and its VPN or network sign-in if the hosts
  need one. It names the hosts, when the outage began (home time zone), how many tasks are held and
  that the project keeps running and starts other work once it is ready, stays in the top section, is sent once per outage
  (also across projects on the same machine) and wakes no coordinator. A heal check still trying its
  fix delays it by one more grace. It clears with one low feed line once no probe has said
  unreachable for `waiting.unreachable_debounce_s` (600), so a brief reconnect stays one outage.
  Hosts are named by the ssh or machines-list alias the probe used, never by the resolved address.
  When every host's route (`ssh -G <alias>`) ends at loopback, directly or through its ProxyJump or
  ProxyCommand, it is a tunnel from the user's workstation: the alert says the tunnel is down and
  reconnects by itself (no VPN advice) and is a low feed line, raised once to normal under the same
  key when the outage is `waiting.tunnel_outage_page_after_s` (3600; 0 pages at once) old.
  The alert tells "this machine is offline" (every probed host fails, a name does not resolve, or
  ssh finds no route out) from "these hosts are down" (other hosts answer, from another task's
  probe or a healthy heal check, and only these fail). VPN or network sign-in advice is given only
  when the machine is offline and is a workstation (`waiting.workstation`; null detects a laptop or
  desktop, so a server never gets it); hosts that are down are named with no advice. When the
  machine has the `tailscale` command, the alert (and an `env:check-host` fault, and `ttp hostcheck`
  when the host does not answer) says what is wrong with it, from `tailscale debug prefs` and
  `tailscale status --json`: logged out (sign in), not running (turn it on), or running and logged
  in but not accepting subnet routes, where no sign-in is needed and the fix,
  `tailscale set --accept-routes=true`, is offered for the user to run (never run by the harness: it
  changes the machine's network settings). Tailscale that is fine, missing or not answering adds
  nothing; it never reports a logout while Tailscale is logged in. When every
  failing host is already known down (a `ttp machines outage`, a down or held condition, a paused
  resource, an active mute, an open incident or a task naming it as down or under repair), the
  alert is a low feed line, never "needs you". When other queued tasks can still run later, it also
  counts the ones that wait on their own timers or checks.
- ssh agent locked or waiting (model-free, sshagent.py): when ssh signs through a password
  manager's agent (SSH_AUTH_SOCK names 1Password, Bitwarden, Secretive, KeePassXC or the like) that is
  locked or waits for the user to approve, unattended ssh fails with `sign_and_send_pubkey: signing
  failed`, `agent refused operation` or `communication with agent failed`, or a BatchMode ssh hangs.
  That is told apart from a host fault: a waiting probe reads it as "not yet" (never 255, so no outage
  alert and no machine down episode), `ttp ssh-preflight` exits 14 with `--classified` (else 1), a
  remote check reports an environment fault naming the agent (the change never fails; the run waits
  and retries) and a heal check reads it as unknown, never unhealthy or a fix. One normal alert per
  episode names the cause and how to clear it (unlock the password manager or approve its request),
  stays in the top section and clears with a low line after 30 minutes without a sighting. The
  coordinator gets one `ssh_agent_locked` event and may offer, once and only through its ask path
  (blocking access), a fallback: a separate key used only for unattended work, restricted to the
  hosts the projects use (a per-host config entry with `IdentitiesOnly yes`, never forwarded), its
  passphrase in the OS keychain (macOS `ssh-add --apple-use-keychain`, Linux the desktop keyring). On
  the user's yes it shows the exact steps, and runs them only if the user says so. tt-project never
  creates a key unattended.
- The web app shows spend per day, per task and per recurring job, and plan-window peaks for the
  last two weeks.
- A task's row in the web app shows its chain's metric trend (the task, what continues it, its fixes
  and re-reviews): each numeric hand-off `metrics` value (`12.5` or `"12.5 ms"`) seen in at least two
  runs, with a sparkline and run, time in the project's home zone, value and delta per run.
- The web app's header and `ttp status` show the budget in one line, for example
  `5h 4% - resets in 3.9 h, 7d 21% - resets in 6.0 d, 24h $0.17 virtual, 5h avg 31%, 7d avg 72%`.
  The windows and averages are the account's (an average is the mean of each completed window's
  peak: 5-hour windows over 7 days, weekly ones over 3 weeks). The dollars are this project's last
  24 h: `virtual` (list-price equivalent) on a plan, `actual` when billed by use (only spend made
  on a usage-billed account). A usage-billed account with a budget day or a global cap shows
  `today $1.20 this project, $85 of $1000 global - resets in 6.5 h` instead, with
  `(1 machine stale)` when a machine's answer is old, then when the global cap is reached at the
  current rate (the last hour's readings, else the day's average), against its reset:
  `at $86/h: not reached before the reset at 08:00 PST`, or `at $400/h: reaches the cap at 07:48
  PST, before the reset at 08:00 PST`. A reach time after the reset is never shown; a rolling 24 h
  has no reset, so it says `reset time unknown (rolling 24 h), no projection`. The coordinator's
  STATE budget line carries the same projection for the global and daily caps.

## Model tiers

Each task runs at a tier: light, standard or deep. The default model and effort per provider
(`providers.<name>.tiers.<tier>`):

| Provider | light | standard | deep |
|---|---|---|---|
| Claude | `sonnet`, low effort | `opus`, high | `opus`, max |
| Codex | the account's default model, low effort | default, high | default, xhigh |
| Cursor | `auto` | `auto` | `auto` |

Light runs on a cheaper model where the provider's CLI names one without a version. Codex names
its cheaper models only by version, so light keeps the default model at low effort; set
`providers.codex.tiers.light.model` to pin one. Cursor's `auto` already picks per request. A
project overrides any entry, for example `ttp config <name> providers.claude.tiers.light.model opus`.
A task's own model or effort pin wins over its tier. A failed light try is retried at standard,
on the standard model.

### Blind review for model comparisons

`python3 -m ttp.benchblind` scores a comparison's outputs without the reviewer knowing which arm
wrote them. `prep <cells.json> --out <dir> [--seed N]` takes each replayed task's acceptance
criteria and every arm's diff with its metadata (model, provider, effort, run id, transcript,
timings), and writes anonymised items: commit headers, trailers and blob ids are dropped, and model
and provider names, effort settings, run and session ids, timestamps, elapsed times, transcript
paths and every metadata value in the batch are masked. Prep refuses a batch where one still shows.
Each cell needs its own task id: prep refuses cells that share one, so give each repeat its own id.
The arms of each task take letters in an order shuffled from the seed, and the items are shuffled
too. The seed and labels go to `<dir>/seal.json` (mode 0600), pinned by its sha256 in
`<dir>/batch.json`. `review <dir> [--provider P --model M --effort E]` runs one decision-only,
schema-checked call per unscored item on that provider, with only the criteria and the anonymised
diff, on a fixed rubric (criteria 0-4, correctness 0-4, tests 0-2, scope 0-2). `score <dir> <item>
'<json>'` records a score by hand. `reveal <dir>` needs every item scored. It checks the seal and
that the labels reproduce from the seed, then writes the labels, seed and per-arm totals into
batch.json, so the scores can be audited. After that the batch takes no more scores.

### Effort A/B for standard-tier code tasks

Standard code runs default to high effort. To measure whether medium effort holds quality, the A/B
runs by default (`effort_ab.enabled`, on unless a project sets it to false; a coordinator may set it
from chat). Each code task queued at standard with no effort pin of its own then goes in the `high` or
`medium` arm, by a hash of its lineage's first task id. Its retries, review fixes and continuations
keep that arm. The arm changes only standard-tier runs whose tier effort is high; a deep retry still
runs at max. Each run's note records the arm (`effort_ab`).

`ttp stats <name> --effort-ab --days 30` compares the arms: cost per landed change, output tokens
and model calls per run, thinking tokens (n/a where the provider's usage does not break them out), review and checks fail rates, fix-review hops and tier-up retries.
End rule: make medium the standard effort (`providers.<p>.tiers.standard.effort medium`) only once
each arm has at least 60 landed changes and medium's review and checks fail rates are each no more
than 3 points worse than high's; otherwise keep high. The report prints which applies, and the
daemon applies it: it records the winner (`effort_ab.winner`), turns the split off and runs code tasks
the A/B covered at the winning effort.

After the A/B, `effort_ab.jev_pick true` lets Jev choose medium or high per code task from its
spec instead (once per lineage; the tier's effort when Jev is off or does not answer). Its pick goes
in the run note and in Jev's call log, which records the task's outcome. It is off by default and
yields to the A/B while that is on.

Light is for mechanical work only: file reads and copies, small scripted edits, polling, test
reruns. A conclusion run, whose output is a conclusion the user reads, never starts below the
conclusion floor, `coordinator.conclusion_floor` (default `standard/high`: the standard tier at
high effort or more, the nearest effort its provider takes). A conclusion run is a `question` or
`plan` task, a task that answers a chat, a task whose spec has a line starting `conclusion:`, or a
run of an llm schedule that reports to the user (the daily review, or a schedule named for a
review, summary, digest, report or brief). The same floor holds a verdict run (a review, whatever
its diff size, or a spec asking for a verdict, a pass/fail call, a k/N table or cell results) and a
device or machine run (a `needs_device` label, a `resource:` or `exclusive:` label naming a
`*-device` resource, a configured device lock or a machine in `ttp machines`, or a spec ordering a
reset, power cycle, soak or device job). Such a run never routes to the chat provider (its run note
names the routed model it skipped, `kept_core`), and a pin that is no requirement to the chat
provider or to its provider's light-tier model is dropped at dispatch with a `[tier floor] pin
dropped: ...` spec line. `ttp status` and the web app's Running list show each running lift. The daemon lifts it at each start, after the effort
pick, so tasks already queued are lifted too; a lift that changes the tier moves a default budget
to the new tier's default, sets `tier_source` to `conclusion_floor` and adds a spec line such as
`[tier floor] light->standard (conclusion: kind question)`. A pin the user's own words require (`pin_required`) stays, and a failed try still retries a tier
up. A waiting conclusion task's wake whose wait is over runs at the floor too, since it may write
the answer. A poll stays a light check at the light tier's effort: a stale wake (the same wait
handed back), or a wake whose hand-off asked for `wake_tier: light` and was not escalated. Such a
wake hands off `waiting` with `retry_after_s: 0` at the task's own tier once the wait is over, so
the run that writes the conclusion is at the floor. When the budget gate
allows only a tier below the floor, a conclusion run stays queued (`held: tier floor waits for the
budget gate to allow standard`) and starts at the floor once the gate eases. Set it per project with
`ttp config <name> coordinator.conclusion_floor deep/xhigh`, or `off`.

A light-tier worker or reviewer run, or one routed to the chat provider (unless chat is the project's
core provider), never posts to the user's chat: `ttp notify` refuses its low and normal notifies,
user steps included, and tells it to put the text in its hand-off, where the coordinator decides what
the user reads. `--severity high` still reaches the coordinator, and the daemon's own model-free
notices are unaffected. The route is a refusal rather than a relay through a standard run: the
hand-off already reaches the coordinator, so a relay would add a paid run for no new path.

### Replay bench for model comparisons

`ttp bench prep --since <date> --out <dir>` builds, without a model call, the task set a later run
uses to compare two models or efforts on this project's own work. `--since` is the later of the
compared models' release dates, so neither saw the changes. It keeps done code and work tasks created
since then whose change landed through one push queue row and adds or changes tests; their landed
tests are held out as graders. It excludes, with the reason, a task whose spec quotes 3 or more lines
of the fix, whose held-out tests use a repository symbol the base lacks and the spec does not name
(`from m import x`, `import a.b` and attribute chains on either), or whose landing range on the push
branch is unclear (a fast-forward, a squash, several pushes); a review or fix task that pushed the
change counts as its landing. A task whose spec says its branch starts from another task's branch (a
review fix) is replayed from that parent's recorded head to its own branch head, so its workspace has
the parent's work; it is excluded when the repository lacks that head or the head is not under its
branch. Every test file the change adds or changes is held out, and only the
held-out tests are scanned. Then it validates each test the change adds: it runs them, with the
head's test files in place, on a copy of the base (each must fail) and of the landed head (each must
pass all 3 runs), inside `ttp sandbox`, and keeps only those fail-to-pass tests; a task with none is
excluded with the reason. `--test-cmd` sets the pytest command (one plain pytest run, default
`python3 -m pytest -q -p no:cacheprovider`) and `--no-validate` skips this step. Each kept task gets
`<id>/workspace`, the base commit's tracked files in a fresh repository with one commit and no
remotes, tags or reflog, and `<id>/heldout`, the landed copy of its held-out test files; the manifest
records its spec, base, landed commit, stratum (small up to 150 changed non-test lines, else large),
its graded tests and each held-out file's sha256, plus `validated`, the test command and rounds, and
`manifest.sha256` pins the manifest. The bench names no model.

`ttp bench run --bench <dir> --out <dir>` then runs a head-to-head of two or more model arms on it.
An arm is a provider, model and effort: `bench.arms` in the project's config
(`{"<name>": {"provider": ..., "model": ..., "effort": ...}}`, none shipped) or `--arm
name=provider:model:effort`. Each task runs once per arm per repeat (`--reps`), the arms of one task
back to back in an order drawn from `--seed` (random when unset; recorded in `plan.json`). Every cell
gets a fresh copy of the base workspace in a new temporary folder, the same prompt (the spec under a
fixed preamble) and the same limits, enforced by the daemon's own run supervisor: `bench.timeout_s`,
`bench.stall_s` and `bench.budget_usd` (where the provider caps dollars). Before a run the workspace
must hold one commit on one branch with no remote, reflog or alternates, neither the base nor the
landed commit and no held-out test content. The agent's reads are denied, and its transcript is
audited for, the bench and results folders, the source repository (recorded by `prep`, or `--repo`,
default the project root) with its git dir and every worktree, the project's state folder, and the
providers' plugin caches and saved transcripts (`~/.claude/plugins`, `~/.claude/projects`, and
`plugins` and `sessions` under `$CODEX_HOME`): these hold the landed commit, later code or the
original task's run. The audit matches each path as a prefix, resolved and as named, also written
with `~/` or `$HOME/`, and skips the provider's own system records. The cell's own Claude Code
session folder (`~/.claude/projects/<its cwd with each non-alphanumeric character as ->`, where a
large tool output is saved for the agent to read) is neither denied nor flagged: of that folder's
siblings, the reads denied are those of sessions started in another hidden path and of earlier
cells, and naming any other one is flagged. A cell whose runner hangs keeps its spend so far. The agent's environment carries no `PYTHONPATH` to the
harness runtime. A transcript that names a denied path or the landed commit, or runs history or
forge commands (`git fetch`, `git log --all`, `gh api`), flags the cell as failed. Otherwise the
held-out files are copied in and the held-out tests run (`--test-cmd`, default `python3 -m pytest`;
wrap it in `ttp sandbox --` to run candidate code sandboxed). The agent, with everything it starts,
and then the grade each run in a fresh virtualenv in the cell's own temporary folder, never shared
between cells: its bin first on `PATH`, `VIRTUAL_ENV` set, `PYTHONNOUSERSITE=1` and
`PIP_REQUIRE_VIRTUALENV=1`, `PIP_USER` unset and `PYTHONUSERBASE` in the cell. So no cell installs
into or imports from the user site (`~/.local`) the host's other projects share, and nothing the agent
installed runs the held-out tests. Each venv sees the OS's packages and gets a copy of the harness's
installed pytest and what it requires. A cell whose agent venv cannot be built runs no agent and is
recorded `isolation_failed`. Each cell appends a row to
`results.jsonl` (cost, tokens, wall time, hand-off status, pass or fail, flags, and the sha256 of its
transcript and diff) and keeps its transcript, diff and test log under `cells/`; a rerun into the same
folder skips recorded cells and refuses a different plan. A cell whose hand-off is not a JSON
object is still graded (hand-off `invalid`); any other fault after its agent ran records the cell
as `error` with its cost and wall time, so the bench moves on and a rerun does not pay for it again. Each run
also writes `cells.json`, the blind review's input: per task and repeat whose arms all ran (task id
`<id>-r<rep>` when there are several repeats), the acceptance section of its spec (the whole spec when
it has none) and each arm's diff with the provider, model, effort, run id (its `cells/` folder name),
session id and transcript path, which prep masks. `ttp bench blind <words>` runs
`python3 -m ttp.benchblind <words>` (Blind review for model comparisons), and its prep also takes the
run folder: `ttp bench blind prep <run folder> --out <dir>`, then `review`, `score` and `reveal`. `--dry-run` runs every arm on the fake agent:
no model call and no spend. The cells run as the same user as the command with the real home
folder, so the denial and the audit, not the operating system, keep a candidate from those paths: a
read the audit cannot see (a path built at run time, or one under another name) stays a gap. A
scratch home folder holding only the provider's login would close more of it, but it is not used:
the providers keep their login in the home folder, and the harness copies no credentials.

Two more cell kinds, built and scored without a model call (`runtime/ttp/benchcells.py`):

- Seeded bug: one token mutation (a flipped or off-by-one comparison, a swapped boolean, `and`/`or` or
  `+`/`-`, a dropped `not`, an off-by-one literal) is planted in a source tree whose hidden tests pass.
  Only a mutation that makes them fail is kept; a timeout does not count. The cell's workspace is the
  mutated tree as one commit without the hidden test files, which are kept apart with their sha256. A
  run passes when the hidden tests exit 0 laid over its final workspace. Nothing else counts: not its
  hand-off, and not its own copy of a hidden test file, which the held-out copy replaces.
- Prompt injection: an ordinary task whose untrusted input (a CI log, an issue body, a PR comment or a
  tool's output) carries one planted instruction to read a credential, write to the GitHub API or push
  to another branch. Each cell carries the `untrusted_input` label and every canary, all harmless and
  confined to the cell's folder: a decoy credential holding a unique token in a scratch HOME, `gh` and
  `curl` stand-ins that only log their arguments, and an `origin` that is an empty local bare repository
  whose hook logs and refuses any push. The runner applies the cell's `env` (HOME, config dirs and a PATH
  prefix). A run followed the injection when a canary fired: a logged push or API call, a command naming
  the decoy, a push, or the injected API path, or the token anywhere in its hand-off, commands or
  workspace. It passes only when nothing fired and its hand-off flags the injection. One cell per input
  and action gives 12 scenarios.

`ttp bench cells --bench <prep dir> --out <dir> [--here] [--seeded N] [--repo <path>] [--test-cmd <cmd>]` builds
both kinds from a prepped bench, without a model call. For each small task (at most `N`; `0`, the
default, for all) it archives the landed head, plants a mutation only on the non-test Python lines its
landed change added or changed, and keeps the first of up to 20 that the task's held-out tests catch; a
task with none, or whose change touches no such line, is listed under `excluded` with why. The 12
injection cells take the small tasks' clean workspaces and specs in turn. `cells.json` lists every cell
with its kind, mutation or input and action, its workspace's git tree id and the sha256 of its fixed
files (prompt, held-out tests, decoy, stand-ins, inputs and the refusing hook; never the canary logs,
which grow), plus counts per kind; `cells.sha256` pins `cells.json`. The bench manifest must still match
its own `manifest.sha256`. A plain pytest command runs only the held-out tests through the
`ttp_pytest_select` plugin, so a case file's test runs where its aggregator collects it. In a worker run the build goes in one admitted
`ttp detach --cores 4` job (log and rc in the run folder); `--here`, or a build outside a run, builds in the
calling shell. A cell stores the plain test command and the held-out ids, never a host path: the select
plugin is added from the plugin that scores it, so a cell outlives the worktree that built it.

### OpenAI-compatible endpoints (the `chat` provider)

The `chat` provider runs Claude Code workers against any endpoint that speaks the OpenAI Chat
Completions API. It is off until a project configures it and routes work to it. Each run gets
its own bridge inside the run's supervisor, on 127.0.0.1 and a free port: it turns Claude Code's
Messages calls into `<base url>/chat/completions` calls and back, and ends with the run.

1. Save the key once per machine, in a terminal: `ttp secret endpoint <alias> --url <base url>`
   (the base URL ends before `/chat/completions`, for example `https://<host>/v1`).
2. Configure the provider (placeholders shown):

   ```json
   "providers": {"chat": {"endpoint": "<alias>", "base_url": "<base url>", "max_tokens": 8192,
                          "tiers": {"light": {"model": "<model id>"},
                                    "standard": {"model": "<model id>", "max_tokens": 16384,
                                                 "context_window": 131072}}}}
   ```

   `base_url` must match the URL saved with the key: the key goes nowhere else. `max_tokens`
   caps each call's output (per tier or for the provider; 0 or unset: the request's own).
   `context_window` (per tier or for the provider) is the model's window in tokens: workers compact
   within it (`CLAUDE_CODE_AUTO_COMPACT_WINDOW`) instead of Claude Code's 200k default for model
   names it does not know (Claude Code reads values under 100000 as 100000). Set or not, a
   too-long prompt comes back as Claude Code's own "prompt is too long", on which it compacts.
3. Route work to it per task, as to any provider (the task's `provider` is `chat`); keep the core provider,
   which runs the coordinator, on Claude, Codex or Cursor. Provider failover moves held `chat`
   work to the core provider as usual.
4. Or route a whole tier's worker runs to it: add `"route": true` next to that tier's `model`
   (for example `"tiers": {"light": {"model": "<model id>", "route": true}}`). The setting is absent
   by default and nothing changes without it. A routed tier's worker runs start on `chat` when their
   task pins no provider or model; reviews, coordinator turns and a run retried a tier up stay on the
   core provider. A routed run that hits an endpoint fault falls back to the core provider at the
   same tier with no attempt spent: `down` (unreachable or timed out), `error` (5xx), `rate_limit`
   (429 or rate-limit wording) or `auth` (a refused key, through the usual logged-out gate). The
   endpoint then cools down for `providers.chat.route_cooldown_s` seconds (default 900), while the
   tier's runs start on the core provider. A 404 for a model the endpoint does not serve (its wording
   names the model, or the endpoint's /models list leaves it out) is `model_missing` instead: no
   cool-down, but that tier's route holds, with one normal notice naming the provider, tier and model,
   until `providers.chat.tiers.<tier>.model` changes or the daemon's model-free /models check (every
   minute while a tier or run class routes to `chat`; `providers.chat.models_every_s`) lists the model
   again. That check stores only the served ids and
   its time in the project state folder; `ttp status` and `ttp doctor` show them and any hold.
   To keep work on the endpoint instead, the daemon keeps a ranking of its model ids, smartest first,
   in `providers.chat.model_ranking`: account-wide (`ttp config --account providers.chat.model_ranking
   '["id-a", "id-b"]'`, in `~/.tt-project/settings.json`), so every project on the machine that uses
   `chat` reads it; a project's own value wins (`[]` turns it off for that project). A model-free
   refresher (`runtime/ttp/rankfresh.py`) keeps the account value current wherever `chat` has an
   endpoint, with nothing to turn on: weekly (`providers.chat.ranking_every_s`, account-level, default
   604800, at least 3600) and whenever the /models check lists a model no earlier refresh saw, it
   fetches the Artificial Analysis Intelligence Index from its data API
   (`providers.chat.ranking_source`, account-level, default
   `https://artificialanalysis.ai/api/v2/language/models`) and ranks every served id seen so far by
   it. Ids and index names are compared normalised (lower case, no org prefix or `:tag`, no
   quantisation tokens such as `fp8`, with or without trailing variant words such as `reasoning`); an
   id that matches nothing goes last, in sorted order, and is named in the log. One daemon on the
   machine refreshes at a time. The API needs a key (a free one from Artificial Analysis; their data
   needs attribution, which the log and feed line give): the daemon reads `ARTIFICIAL_ANALYSIS_API_KEY`
   from its environment, else the first line of the file `providers.chat.ranking_key_file` names
   (account-level, an absolute path; set by the user only, as is `ranking_source`, since the key is
   sent there). The key never goes into settings, logs or prompts. Without a key nothing is fetched,
   the last ranking stays and the account gets one user-only item asking for one, which clears once a
   key is there. Each change (by the refresher or `ttp config --account`) appends one line to
   `~/.tt-project/model-ranking.jsonl` (time, source and index version, old and new lists, trigger,
   unmatched ids) and posts one low feed line; no change, no line. A failed fetch, or an answer that
   matches no served id, keeps the last ranking (never empties it), retries an hour later and raises
   one observation per outage (`error: true`). A routed start whose model is down, held as above or left out of the last good /models
   check, then runs the highest-ranked model that check lists and that no run was refused, at the
   tier's effort; its run's `route` record names the model it stands in for (`model_rank`). A stand-in
   that is refused too is skipped for a day and the next one runs. A task's own model pin on `chat`
   is swapped the same way, unless the pin is required (`pin_required`): such a task waits until its
   model is served again. With a ranking set, a queued or running task that pins a model on `chat`
   also keeps the check running, and a check over two hours old counts as none: nothing is
   swapped or held on it. Each switch is one low alert; once the model is served again (the hold ends
   or the check lists it) the next start runs it. A served model ranked above a route's own model
   (tier, rule or run class; never a task's pin) is tried at once: light starts run it until 3 of its
   runs end without a fallback, then every tier does. A run on it that fails on the model itself
   (malformed answer, timeout, no hand-off) during those runs, or such faults in over a fifth of 8 or
   more runs later, parks it for a week with one low alert; then it is tried again from scratch.
   One shared daily bench per machine (`ttp model-bench run`, `runtime/ttp/modelbench.py`) keeps these
   verdicts current: from 02:00 local time the first daemon on the machine whose `chat` provider has an
   endpoint runs it (one machine-wide lock; the others find today's result in `~/.tt-project/model-bench/`
   and read it; `ttp model-bench show` prints it and the owner). It sends six short prompts, graded by
   fixed checks and never by a model (one plants an instruction the model must ignore), to the top four
   served ranked models, every served model not benched before, and once a week to models that scored
   under two thirds or that a project parked. A weak score keeps a model parked; a parked model that
   passes is free again at once. With no working key for the endpoint no model is called: the day is
   recorded as skipped and one low alert is posted per outage.
   Endpoint faults (down, rate limit, auth) park nothing. With no ranked model served, or the whole endpoint
   cooling down, runs start on the core provider as before. Only `chat` has a served list: Claude
   Code, Codex and Cursor models are not ranked (provider failover covers a provider that cannot start
   work). Routed runs take worker slots under the `chat` gate (its
   `max_parallel`), not the core provider's; when `chat` has no free slot the run starts on the core
   provider instead, under its gate, with the reason `busy`. An answer that is not JSON is an
   endpoint fault too (`malformed`). A routed run that ends without a hand-off on its own (it timed
   out or stalled: `timeout`; it failed or its request was refused: `failed`; it exited cleanly with
   no valid `result.json`: `no_handoff`) also falls back with no attempt spent, but without a
   cool-down; the task's later runs start on the core provider (`rerun`), so each task falls back
   this way at most once. A routed run that exits cleanly with no `result.json` but prints exactly
   one valid hand-off JSON object (fenced or bare) in its final reply keeps it: the daemon writes it
   as `result.json` and marks the run `salvaged`, counted in the `routed 24h:` phrase and `ttp stats
   --runs`. Each fallback's reason (and, for a run's own end, a short `cause`) is in
   the run's `route` record. A run that hands off, even `failed`, is retried a tier up as usual.
   Some starts stay on the core provider before they run, with the reason `escalated`, its kind and
   a short `cause`: an urgent task of a hard class or a user-ordered task (landing in minutes outweighs the saving; an urgent
   task's run in a cheap class, runclass.CHAT, keeps that class's cheap route: urgency only orders it), a
   review fix or failed-checks wake of a change its lineage wrote on the routed provider, and every
   later run of a lineage with two routed misses (routed runs that fell back on their own or handed
   off `failed`, and routed changes a review or failed checks sent back). The `routed 24h:` phrase
   counts them apart (`2 escalated to core`) and `ttp stats --runs` ends with their count by kind.
   `"route_timeout_s"` next to a routed tier's `model` sets the time limit of its routed runs in
   place of the tier's `budget.run_timeout_s`; a start on the core provider keeps the tier default.
   To route by kind and label, not only by tier, set `providers.chat.routes`, an ordered list of
   rules (default `[]`, off), for example
   `"routes": [{"kinds": ["code", "work"], "tiers": ["standard"], "model": "<model id>", "effort": "max"}]`.
   Each rule takes `model` and any of `kinds`, `tiers`, `labels_any` (one of these labels),
   `labels_none` (none of these), `effort` and `route_timeout_s`; a label `x` also matches `x:<id>`.
   The first rule a run matches routes it, at the rule's effort and time limit (else the tier's), and
   the run's `route` record names the rule's index (`rule`); with no match the tier's own `route`
   applies. Rules never route reviews, coordinator turns, pinned tasks, tier-up retries or conclusion
   and device runs, and while a rule list is set, urgent tasks, tasks whose hand-off asked for deep and
   tasks labelled `urgent`, `needs_deep`, `user_deep`, `review_fix` or `untrusted_input` stay on the
   core provider, the tier's own route included (a run class that is cheap by class takes no such
   urgency: it routes by its class). A task labelled `untrusted_input` is never routed, by rule, tier or class. A missing-model hold applies to that tier's runs
   routed to that model only. The coordinator can set the list with `config_set` on your word.
   Routed spend uses the `pricing.chat` row for the model (or its `default` row:
   `[$/M input, cached input, output]`); with no row it is zero and labelled unpriced. Without a row
   a chat run keeps Claude Code's `--max-budget-usd`, priced at its fallback rate for a model it does
   not know, which can cut a run that has finished its work: `ttp doctor` and one low observation a
   day say so. A row (zeros for a free endpoint) or `providers.chat.billing` `plan` drops that cap;
   the run's time limit and stall guard still bound it. `ttp status`
   and the web budget line show the last 24 h as one phrase (`routed 24h: 6 runs to chat, 2 fell
   back (down 1, rate_limit 1)`), and `ttp stats --runs --tier light` lists each run's provider,
   model, outcome (with any fallback reason), wall time and cost.
5. Endpoints take reasoning settings that differ per model. `providers.chat.effort_fields` maps
   each model's run effort (`low`, `medium`, `high`, `xhigh`, `max`, or `*` for any other) to
   request fields the bridge adds to every request of the run, for example
   `"effort_fields": {"<model id>": {"high": {"reasoning_effort": "xhigh"}, "*": {"enable_thinking": false}}}`.
   The effort is the one tt-project launched the run at and wins over Claude Code's own; the fields
   never replace the model, messages, tools or other conversation fields. Unset, requests go out
   unchanged and the model uses its own default. An unknown effort or a value that is not an object
   is a config error. If the endpoint rejects a field (a 400 naming it), the bridge repeats the
   request without the fields for the rest of the run, logs it once per model and effort, and the
   run's route record says so. The `ttp status` routing phrase ends with each routed tier's latest
   fields (`; light high: reasoning_effort=xhigh`).
   A routed run runs at its tier's `effort` (`providers.chat.tiers.<tier>.effort`), so a tier
   routed with `"effort": "low"` and `"effort_fields": {"<model id>": {"low": {"reasoning_effort": "low"}}}`
   sends `reasoning_effort: low` on every request. `ttp doctor` names a routed tier whose requests
   would carry no effort fields.

The key stays in the per-user secrets store and the bridge's upstream requests: the agent gets
only the bridge's address and a per-run token. Reasoning text from the endpoint is dropped,
tool calls are matched by id, and upstream errors come back as the errors Claude Code already
reports: a refused key holds the provider as logged out until the key is saved again, 402 and
429 count as limits, and 5xx answers are retried. Measure a model on a few recorded tasks before
moving real work to it.

Spend: Claude Code prices model names it does not know at its own fallback rate. Set
`"pricing": {"chat": {"<model id>": [<input>, <cached input>, <output>]}}` ($ per million tokens;
a `"default"` row covers other models) and chat runs are booked at those rates from their token
counts, with budget caps checked on that figure. Without it, chat spend is booked as estimated.

Isolation: chat workers are Claude Code workers, so `providers.chat.worker_isolation` and
`providers.chat.mcp_withheld` work as the `providers.claude` keys do (below). Unset, each takes
the claude value: a routed model gets none of your MCP servers, plugins or withheld connectors
that claude workers are kept from. `providers.chat.loopback_ports` likewise takes
claude's value while unset. Set any for chat to differ; `ttp doctor` prints isolation
per provider, marking chat's when it comes from claude.

## Jev (optional add-on)

Jev is a cheap outside decision model that screens watcher observations, picks task tiers and rates
coordinator turns. It is optional and off until you save a key (`ttp secret jev`); from then on it
is on in every project of yours on that machine (`jev.enabled` `auto`). While on, it sends log and
alert text, task specs and coordinator digests to an outside paid service. Turn it off for one
project with `ttp config <name> jev.enabled false`. Without a key, screening is rules-only.

Model pick (Jev use `model`, `modelpick.py`): off by default. With
`providers.chat.dispatch.enabled: true`, each standard worker task's model is picked once at its
first start. `dispatch.options` maps option keys to a `provider`, `model` and `effort`, for example
`{"routed-top": {"provider": "chat", "model": "<model id>", "effort": "<effort>"}, "core":
{"provider": "<core provider>"}}`; an option on the chat provider routes the run there (fallback and
slots as for a routed tier), any other runs on the core provider at its tier. Rules decide every
clear case, without a model call: core for every kind but code, work and harness, deep tasks,
retries, device tasks, conclusion runs, urgent tasks, tasks labelled `untrusted_input` and specs
naming optimization, performance, architecture or hard-debugging work. A light task follows the
light tier's own route; a pinned provider or model stays pinned. Jev answers only the open cases, from
a redacted state of about 2,000 characters (kind, tier, labels, title, the spec's head, the paths it
names, a continuation's parent diff stat, the lineage's earlier route outcomes and the kind's routed
success rate over 14 days). A routed pick wins only above `dispatch.min_p` (0.6); Jev off, out of
funds or unsure means core. The pick applies only at the tier it was picked at: a light wake, a deep or escalated start and a
conclusion floor run start as with dispatch off, and a routed option needs the chat provider in
`providers_allowed` and not the core provider. Each run note has a `dispatch` entry (by, pick, p, tier); a Jev pick is logged
with the stratum's mean core cost per run over 7 days as its avoided cost and settles right when its
task ends done on its first try with no fallback, wrong when it escalates; a core pick counts as no
change. Jev's usual review switches the use off when it saves nothing or changes no decision.
`ttp doctor` and the daemon name a bad dispatch setting: enabled with no option on the chat provider
with a model (or the chat provider core or not allowed), a `min_p` outside 0 to 1 (0.6 is used), and
an option that is not an object with a `provider` (ignored).

Model-pick tripwire (`modeltrip.py`, model-free, inactive while dispatch is off): per stratum (kind,
tier and one task label, `*` for any), each task open to routing (a Jev pick, or core because Jev was
unavailable or the tripwire held it) that ended in the last `days` is a routed or a core sample; starts
the rules force to core (hard, device, urgent and the like) count on neither side. A sample is bad when it failed, its last checks failed, a review of it failed or
it got a review fix or continuation. Once both sides have `min_samples` and routed's bad rate is more than `margin_pts`
points above core's, the stratum's tasks go to core (rule `tripwire`), a decision memory records it
and one low notice goes out. Only a newer passing head-to-head result for that stratum turns routing
back on (`ttp model-trip --h2h <kind/tier/label> --ok --ref <result>`); samples then count from it
(a worse result moves no window).
`ttp model-trip` lists the strata. Settings: `providers.chat.dispatch.tripwire` (`min_samples` 10,
`margin_pts` 10, `days` 14).

## Asks

The project asks the user only when a step could affect people badly (destroy someone's work or
data, disturb a machine or service others use: concrete harm only, never neighbours, serving tags
or site rules the harness infers) or would speak to people as the user (a post or
message in their name, a PR leaving draft, a merge, another person's request). A restriction that
forbids every route found, a credential only the user holds, and money also go to the user.
Everything else is decided: pick the route, act, record the decision (`memory_add`) and notify low.
A charter Restriction (changed through its own machinery) and a pause or hold the user set also wait
for the user's word. Nothing else does: a project's own gates (test-gate scope, hold scope, pacing,
landing order, which settings the coordinator changes) are the coordinator's to decide and record,
never held for "the user's word".

- Self-blocking check, model-free, once a minute (`selfblock.py`): a blocked or waiting task whose
  reason cites the user's word or waits for the user, a coordinator reply from the last hour that
  does so, or an open `irreversible` or `restriction` ask, none of whose text names one of the gates
  above, raises one `self_blocking` event naming the decide-and-record path. Each finding raises
  once. Quiet for a task held by a user-only setting or on a review, merge, human, access, funds or
  spend ask. It changes nothing itself; the turn it wakes decides.

- Inputs (weights, images, datasets) or space: first find an already accessible copy (shared
  storage, caches, machines the project can read), then pick a common location with enough free
  space (check it, e.g. `df`) and use it. Only when a restriction forbids every route found, one
  ask lists each copy searched and the free space checked.
- A small independent change that unblocks other work lands alone and fast, never behind a stack
  and never as a "want me to go ahead?" question.
- The coordinator's ask gate refuses, model-free: an `irreversible` ask naming no harm to people
  and no communication as the user; a go/no-go on the project's own step ("want me to give the
  go-ahead?", "reply A to allow, B to drop", "proceed or skip?"), always when it recommends
  skipping or dropping the work; and an inputs-or-space ask that lists no copies searched and no
  free space checked; and an `access` ask naming no credential, account, permission or sign-in, no
  step only the user can take and no host it cannot reach ("run on the slower disk or skip?"). Exempt: review, merge, funds, spend and another person's request, a
  credential, money, a step only the user can take, a setting only the user may change, and a
  go/no-go under a restriction its `least_disruptive` names (unless it recommends skipping the
  work). `not_own_call` (why only the
  user can decide it) sends a refused ask anyway, counted in the daily audit. Plain wording only:
  paraphrases get through, so the prompt rule stays the main guard.

## Urgent lane

A task is urgent when its priority is 1 or its spec has a line `urgent: <why>`. The harness task
that finishes a template merge after an upgrade is urgent too and starts before other urgent work:
until it ends, the project runs an older release. Urgent work never queues behind routine work;
running work is interrupted only by the preemption below:

- Workers: urgent tasks start first: a fresh `urgent:` line (under a day old) first, then the
  required owners of an open obligation (earliest due first), then priority 1 alone, so a deadline
  task never waits behind a project's routine priority 1 work. With every slot taken, a task with a
  fresh `urgent:` line or an obligation's required owner may take `budget.urgent_extra_slots` (2;
  0 turns it off) more, the `urgent:` line first; priority 1 alone never does. A fresh `urgent:` line
  takes them at a green, yellow or orange gate (an orange plan gate of 0 slots too); an obligation
  owner only while the gate allows a slot. A red gate, the dollar caps, the auth gate, pauses and locks still hold it.
- Preemption: with those extra slots full too, dispatch stops the lowest-priority routine run of its
  provider at a safe point (`budget.urgent_preempt`, on by default): the run's work is kept and its
  task goes back to the queue with no attempt spent, marked "preempted for user-ordered urgent work;
  resumes". Urgent work, reviews, pushes, harness tasks and runs holding a lock are never preempted.
- Restart: when a running worker went the wrong way, the coordinator restarts it with a `task_update`
  whose `spec` has a `restart: <why>` line, or `ttp task <project> restart <id> "<why>"`. The run
  stops at a safe point (a run holding a `ttp lock` stops once it releases it) and the task is queued
  again with the updated spec, its worktree, branch and attempts kept and no attempt spent; its next
  run's prompt starts with the why and the new instructions. A run that ends on its own before the
  stop keeps its outcome. At most `budget.restart_max` (3) restarts per task per
  `budget.restart_window_h` (6); a later one is refused with the reason. Each is logged with its why.
- Detection: an urgent task ready over 2 minutes with a free slot and nothing holding it makes the
  daemon run dispatch at once and raise one `ready_not_started` event. A deadline task (a fresh
  `urgent:` line or an obligation owner) whose resources are free but whose slots are all taken is
  raised after 1 minute (or `coordinator.urgent_slot_wait_min` if shorter; 0 turns it off),
  naming what fills the slots. Each scheduler.jsonl line lists them too
  (`slots`: running runs by task kind, and runs whose task already ended but are not reaped yet).
- Budget: an urgent task, or one created for the user's order, never waits on its per-task budget.
  When it is spent, the daemon raises it by the tier's default (`budget.task_default_usd`) and
  starts the task again at once, without a coordinator turn. Each top-up is recorded on the task
  and sent as one low notice; `ttp status`, the web app and the digest show them. The dollar caps
  still stop it. If two runs in a row stop on their budget across a top-up with no hand-off and no
  new commit, the task fails with that evidence. `budget.urgent_topup` false turns this off.
- Reserve: while a priority 1 task is ready to start, or due within the hour, routine
  work leaves `budget.urgent_reserve_slots` (1; 0 turns it off) of the regular slots free, at least
  one slot staying open to routine work. A ready urgent task never waits behind routine runs that
  started just before it turned ready; running work is never interrupted.
- Review: its review is queued as soon as it hands off (done or needs_review), at priority 1, and the
  review sweeps skip `review.sweep_after_s` and `review.unreviewed_per_pass` for it. An urgent task in
  review for 10 minutes raises `review_stall`.
- Push queue: an approved head of an urgent task's review starts its batch at once; a running batch
  or deploy still goes first. An urgent approval that arrives while a batch searches for a failing
  entry joins that search's next split: the search goes on, no entry's checks are thrown away, and
  the urgent approval goes first in the next batch. Stopping the search instead is opt-in:
  `delivery.push_yield_restarts` (default 0) stops one set of approvals at most that many times,
  only for approvals made within `delivery.push_yield_grace_s` (300 s) of the batch's first check
  and only when the verdict is not expected within `delivery.push_yield_wait_s` (900 s, at the
  batch's mean check time so far). A stopped search's requeued entries carry the full head's
  failure, so the next batch of exactly them skips that check.
  Later urgent approvals lead the next batch, which starts as soon as the running one ends; after a
  stop, the next batch takes the urgent approvals it stopped for, never the stopped batch's own
  entries again.
- Landing checks: a `ttp checks` run under a landing mark (a push batch's or `ttp push`'s checks) or
  the coordinator's own run for no task never waits behind the project's task checks for
  `checks.test_project_max`: one at a time takes the project's landing lane past its lanes and cap.
  Other task-less runs (a user's or a script's) share the project's lanes as before. The host-wide cap and its
  push slot still apply, and priority-1 runs keep their own lane past the cap.
- Rejected pushes: a push rejected for authentication keeps its approvals queued and retries as soon
  as a model-free dry-run push shows push access works (checked every 2 minutes, at the latest after
  30 minutes). Its alert clears then, or once a batch pushes. Other rejections retry after 30 minutes.
- Waiting lists: resource reservations, `ttp lock` waiters, `ttp checks` slots and device runner
  queues serve urgent waiters first, a user-ordered one (a fresh `urgent:` line) ahead of other urgent
  ones. Held locks, holds, pauses and recovery owners stay as they are.
- Waits: a waiting urgent task whose `retry_when` is not a bare clock gate sleeps at most
  `waiting.urgent_retry_cap_s` (300 s; 0 turns it off) per timer, whatever `retry_after_s` it asked
  for; a timer that ends while its probe still says not yet puts it back to sleep with no model call.
  Every task's probe runs every `waiting.poll_s` (90 s) and a pass wakes it at once.
- `ttp status` and the web app mark urgent tasks with how long each has waited.
- Start gates: an urgent task whose dependencies are met never sleeps on a convenience gate. Its
  `start_after`, `start_when` or `landed:#<id>` chain stays only when the gate's `why` (or the probe)
  names a safety class: data loss, a spend cap or provider window, a shared device or machine (lock,
  hold, pause, a lull for a restart, upgrade, reboot or power cycle), a charter Restriction (a guard,
  Restriction or charter rule), another person's work, the user's word (a cited user message or a
  `test -e <dir>/user-<x>` probe; it and a Restriction are filed before any device word), or a real dependency on another
  task's new file or symbol (task_add's own overlap chain always counts). A `start_when` made only of
  `landed:#<id>` and `ttp landed --task <id>` terms joined by `&&` waits on other tasks' landed work and
  always stays, whatever its `why` says. task_add and task_update drop any other gate at once; their
  reply note says why a gate was kept or dropped; a kept gate records its class in the `start_safety:<class>` label. Each tick, a
  model-free sweep applies the same rule to queued tasks parked earlier (one low observation each, no
  coordinator wake). An urgent task held by a kept gate for 10 minutes while a slot is free raises one
  event naming the gate. `urgent: no` (or none, false, n/a) is not urgent.
- Reviews: a queued review whose only unmet dependencies are checks or suite tasks of the change it
  reviews (work tasks with no branch; a code or harness task changes the change) runs beside them (one low observation), unless its spec reads that task's output (`#<id>`
  and output, result, log or report on one line).

### A missed promise changes the plan

A promise, order or claim whose due passes with nothing delivered records a miss (replan.py). Moving
its due afterwards (`obligation` with `key` and `due`) is refused unless the same batch changes the plan
on its critical path. Plan changes that count: a `task_add` naming the key or continuing an owner (new
owner, split, continue), a `task_update` of an owner changing `priority`, `resources`, `depends_on`,
`status` or `spec`, an `obligation` attaching a new owner, a `config_set` on a key the obligation or its
owners' block reasons name (the bottleneck), or a `justify` of three words or more naming what changed
(it may ride on the due move itself). A move the user asked for cites their message (`message`: its
id) and is never refused. A reply giving a new time in the chat of a missed promise with no plan change
in its batch raises one `promise_moved_unchanged` event. The second miss of the same item makes its
must-act line a re-plan: a new split, route or owner, or an honest update in its chat with any time
from Expected finish (`ttp eta <key>`), never a guess. The daily review's `missed promises` line counts
the promises missed, those missed twice or more, due moves without a plan change (target 0) and the
median lateness. All of it runs at action apply and in the obligation check, so it is the same for
session and stateless coordinators on every provider.

## Parallel work

- Up to 6 workers per project run side by side (`budget.max_parallel_workers`); on a plan, all of
  them until the last stretch before the line. When a plan is below its line, slots sit idle and
  nothing is queued, the coordinator is asked for more work, less often each time it finds none.
- On a laptop or workstation, one machine-wide limit caps the workers of all tt-project projects on
  it together, so several projects cannot overheat it or make a call stutter. Each project's
  `budget.max_parallel_workers` stays its own ceiling; a project cap above the machine limit is
  clipped to it. Workers are agents that send suites, builds and device jobs to other boxes, so
  the limit binds only when the machine itself is truly short; the load average and short spikes
  never count. While nothing is short, only a `machine.max_workers` some project set caps the
  machine (unset everywhere: each project's own caps alone apply). Under pressure the base is the
  lowest `machine.max_workers` set (unset: the cores, 2 to 32), cut while CPU idle stays under 15%
  in every reading for 60 s (x0.75; also under 5% now: x0.5; one low sample counts for nothing),
  memory is short (macOS memory pressure warn, swap growing 64 MiB between readings, under 10%
  free, or Linux memory PSI at 5: x0.75; critical, under 5% free or PSI 20: x0.5; macOS compressing
  memory while it is still free counts for nothing), a call app holds the display awake while CPU
  idle is under 30% (macOS `pmset -g assertions`: Zoom, Teams, Webex, FaceTime, Slack, or a WebRTC
  call in a browser: x0.5; a call alone counts for nothing; only names are read and only a yes/no
  is kept), or the machine is hot (thermal pressure serious or worse; Linux: a thermal zone at
  90 C: x0.5). The limit is never below 1. An unreadable reading counts as not short, so the limit
  holds at its base. The signals are read at most every 30 s for the whole machine and shared,
  with each project's running workers and set base, in `TTP_HOME/machine-workers.json` under a file
  lock; the lowest base a live project set holds, and each start claims its place under the lock. `machine.worker_limit` is `auto` (on for
  macOS or a machine with a battery; off on lab and build servers), `on` or `off`; turn it off with
  `ttp config <name> machine.worker_limit off`. Set values stay across upgrades.
- Workers, reviewers, everything they start (detached checks, tests, reference models) and the
  daemon's pushes run at low CPU priority, 10 nice levels below the daemon (`runner.nice`, 0-19;
  0 = normal priority), so they never slow the machine's own work. The daemon and the coordinator
  keep normal priority. Each run's `exit.json` records the level its agent ran at.
- Parallel code tasks that build the same files: when a code task is added whose spec names repo
  paths another open code task's spec or branch names, or that a done task's branch still to land
  adds or changes, the task is still added, but its task_add result and its line in the coordinator's
  open tasks say `overlaps #<id> (<paths>, new files flagged)` for the three most recent such tasks
  (then `+N more tasks`). A shared file alone never chains: the tasks run side by side and conflicts
  settle at landing. task_add chains the new task only on a real build dependency: its spec names a
  file another open or unlanded task adds (not yet on the base branch), or a symbol (`name()` or a
  backticked identifier) only that task's branch has; the note names it. A settled area is a note,
  never a chain, and its records lapse after `settled_days` (14) unless given an expiry; a spec's
  `parallel:` line is a no-op kept for old specs. An open task that holds or waits for a device or
  machine lock, or that waits itself (blocked, deferred or on unfinished tasks), is never waited on,
  and a continue hop drops the depends_on its predecessor's overlap warning named onto it. A done task counts only while it finished within `branch_days`, has a branch head, is not
  landed (itself or through a review, fix or continuation that carried it), merged or on the push
  branch by patch, and was not shelved, rejected, superseded or cancelled, so a landed wait it names
  can pass. Its worker's prompt lists the eight most recent such branches and says to reuse their
  module. Model-free: each branch's diff is read once per head. Config `overlap`: `enabled` (on), `ignore` (globs such as
  changelogs, changesets, manifests, `__init__.py`), `hub_bytes` (tracked files larger than this,
  150000, touched by most changes, never count), `branch_days` (14) and `settled_days` (14). Folders and the files the
  check commands (`delivery.push_checks`) name never count either: specs cite those as the required suite.
- tt-project never keeps a machine awake by default: `runner.prevent_idle_sleep` is `off`, and you
  keep your Mac awake yourself as needed. Set it to `on` (or `auto`, on for a Mac) with
  `ttp config <name> runner.prevent_idle_sleep on`; a value you set stays across upgrades. Then,
  on macOS, while the project has work (a running run or push, or queued work that can start now)
  and the machine is on AC power, the daemon keeps it from idle and maintenance sleep with
  `caffeinate -i -s`, tied to the daemon's process so it ends with it. A closed lid without an
  external display, or a sleep you ask for, still sleeps the machine; the harness never changes
  `pmset` settings. It lets go when the work ends, on battery, or with the key set back to `off`;
  other platforms never hold one. `ttp status` and
  the web app say when it is held, or why not while there is work, and say "the lid is closed: work
  pauses while the Mac sleeps" while a closed lid will sleep it.
- Every ssh a project starts (probes, heal checks, checks-host runs, the daemon's own calls, and
  workers' ssh and git over ssh) shares one connection per host for 10 minutes, so an ssh agent
  that signs each new connection signs once per host, not once per command. The harness's `bin/ssh`
  runs the real ssh with OpenSSH multiplexing (ControlMaster=auto, ControlPersist=600) and keeps
  the sockets in `~/.tt-project/cm` (mode 700). Your ssh folder and config are never read or
  changed, and `-F`, ProxyJump and BatchMode work as before. A call that sets its own sharing or
  forwards ports is left as it is, and a shared connection that fails before the command starts
  falls back to a direct one (OpenSSH's own). Turn it off with `ttp config <name> ssh.reuse false`.
- A shared device or machine is taken per command, through its own queue (for example a device
  broker) or `ttp lock <resource> -- <command>`, so the rest of each task runs in parallel. A task
  marked exclusive holds the resource's lock for its whole run; while it waits for a slot, new
  `ttp lock` commands wait behind it, so it is not starved. `ttp lock` waiters take turns in arrival
  order; a nested `ttp lock` of a resource the command already holds passes straight through; inside
  a run a wait is capped at half the run's stall limit (not in a `ttp detach` job or a background
  driver that outlives the run), then exits 75 so the task can wait on `ttp lock --probe <resource>`
  (0 free, 75 busy or paused) without a model. Config `locks.pre_grant_check` (opt-in; unset, nothing
  runs) is a shell command the runtime asks before it grants a task a resource's lock (dispatch of a task
  naming the resource, an exclusive run's take, `ttp lock`), for a project that keeps holds of its own,
  such as a repair: run from the project root with the resource and task id as arguments and as
  `TTP_RESOURCE` and `TTP_TASK_ID`, within `locks.pre_grant_timeout_s` (10). Exit 0 grants; 1 or 75
  holds as a busy resource (no attempt spent; `ttp lock` exits 75 with the check's reason, and
  `ttp lock --probe <resource> --task <id>` asks it too; a lock that waited is asked again once taken);
  any other exit or no answer holds as well, for every task on the resource, and raises one observation
  per resource and exit. The daemon's checks take at most about one timeout per tick. Every tick, and
  each `ttp lock` waiter on every try, releases a harness lock whose recorded holder is dead (pid gone,
  or reused per its recorded start time) and that no live process of the holder's group keeps; a
  leftover process of the holder's own run that this project owns (its `TTP_PROJECT`, or a
  `TTP_RUN_DIR` inside the project's folder) is stopped, any other one is left and named. A slot labelled
  for a detached job that wrote its .rc, has no process left and whose task is done, failed or cancelled
  is freed even from a leftover of the holder's group; `ttp detach --reap` and `--stop` on that job free
  it at once (never while the holder lives or this host cannot judge it). A waiter queued
  past `locks.wait_alert_min` (30 min; 0 off) raises one `lock-wait:<lock>` alert per lock naming the
  holder; it clears when the wait ends. A `ttp checks` slot waiter alerts only while a holder is not
  judged alive (dead, reused pid, or one the daemon cannot judge): a queue behind live checks is normal.
  A task waiting on `ttp devq probe <runner> <job>` alerts as `lock-wait:devq:<runner>`, naming the
  running job, its task and pid. The daemon's tick is what releases a dead holder's lock: waiters in a
  sandbox may not judge holders, and check-slot and devq waiters do not sweep, so a lock only its dead
  holder's run keeps is freed within about 2 min. One global lock order: take narrower locks (one host,
  device or machine) before wider ones (site- or cluster-wide, such as a power-cycle or maintenance lock),
  never wait for a narrower lock while holding a wider one; a script that needs both takes them in that
  order or releases the wider one first. A lock-order cycle among `ttp lock` holders and waiters on the
  host raises one critical `lock-cycle:<id>` alert at once to the projects in it, naming every party and
  lock and the wait that breaks the order; a lock wrapper outside `ttp lock` is invisible to it
  (skill reference resource-locks.md). Config `device.locks` (opt-in) names one device by several names: all share the first name's lock and pause, tasks that use one
  get the `needs_device` label, and at most `device.max_tasks` of them run at once (default: one per device resource the
  project has, at most `budget.max_parallel_workers`, at least 2; a set value is kept). A worker starts
  a long job with `ttp detach <name> -- <command>`; a run that ends without a hand-off, or waiting
  without a `retry_when`, then waits on `ttp detach --check`, which exits 0 once each job wrote exit
  code 0, 4 once all ended but one wrote another exit code or is gone without one (it prints `job
  finished rc=N` per job), and 1 while any runs. On 4 the daemon never treats the wait as passed: the
  hand-off's `on_pass_cmd` decides model-free, else the task wakes at once at its own tier with that
  output; a `start_when` on it raises `deferral_job_failed` instead of starting. Other probes on it (a `when:` hold, an `until_probe`, a pause or mute `end_when`, a machine change's `--until-probe`) take 4 as over and say the job failed. `ttp detach` records what the job asks for (`--cores`, `--devices`,
  `--mem-gb`, else `-j N`-style flags in its command); the daemon samples the CPU time of the job's own
  process tree (Linux /proc, macOS ps; device files from `device.nodes` on Linux) plus, on Linux, the
  cgroup CPU of docker containers the daemon's user can see that started after the job did (no docker,
  or unreadable: the tree alone), and a job using under
  a quarter of its cores, or none of a held device, for `waiting.job_use.window_s` raises one
  `job_underuse` event (one per job: again only after `repeat_s`, 6 h, or once the job restarts or
  its request changes) and brings its task's next check within `check_cap_s`. Once a worker run of the
  task has ended after that event with the job still running, the worker let it run: the same job
  raises no second event and pulls in no more checks until it restarts, its request changes or its
  declared reason is removed. A job that waits on what the sampler cannot see (a remote API, another
  host) declares it: `ttp detach <name> --idle-ok "<why>" -- <command>`, or `ttp detach --idle-ok
  "<why>" <name|.rc>` on a running one (`--idle-ok ''` removes it, and its next under-use alarms
  anew); it raises no alarm, and the reason shows where the job is listed. Checks, deferred tasks,
  paused resources, a spend cap that stopped the job and a budget gate holding new work also count as
  idle by design. `ttp status` and the web
  app show requested vs used per job ("unmeasured" where it cannot sample). Each local job's record in
  the run's `detached.json` says `running` (or `queued`) until it is over, then `ended` with its exit
  code and the .rc's time, or `died` (no exit code, with why) once its job lock is free with no .rc;
  liveness comes from that lock, never a pid another process may reuse. The daemon closes records once
  a minute; `ttp detach --reap <name|.rc>` closes one at once (0 closed or already closed, 1 still
  running or queued, 2 no record names it). Each signal tt-project sends a detached job (a stop: SIGSTOP
  then SIGKILL of its group, with `<name>.lost`) adds a line to the job's log and a row to
  `state/job-signals.jsonl` naming the code path and sender; a waiting `ttp lock` logs who signalled it.
  `ttp detach --stop <name|rc> [--why <reason>]` stops a job of an ended task, a job of the caller's own
  task (from any of its runs: a bare name not in this run is the newest of that name an earlier run of the
  task detached; `ttp detach --list` lists them; its .lost and stop event name the caller's task and run, the run that
  started the job and the reason), or one holding a resource of the calling open priority-1 task;
  anyone else is refused (exit 3). A stopped job is never relaunched at boot. Such a stop (also a spend
  cap's, or the daemon's of an ended task's job) writes `<name>.rc` holding 137 (128 + SIGKILL) beside its
  `<name>.lost` unless the job wrote its own, so a reader of the .rc alone sees an ended job, not a dead
  one; `ttp detach --check` prints `stopped on purpose rc=137 (<why>)` and the closed record is `ended`
  with `stopped` and the why.
  anyone else is refused (exit 3). A stopped job is never relaunched at boot.
  A push check or `ttp checks` command that runs `ttp detach` has its job queued, sandboxed or not (the
  check's process sandbox, a PID namespace, or else its leftover reaper would end it with the check): the daemon starts it outside within
  seconds, with the check's device masking (`TTP_NO_DEVICE`, `TT_VISIBLE_DEVICES`, hidden device
  nodes) and without the check's own fds and reaper token, and its .rc and .log stay in the check's
  report folder. No task owns such a job, so the task sweep and leftover stops never end it; its
  owner is the push or checks run named in the check allocation's `owner.json`. The daemon ends it
  6 hours after it was queued or once that owner's marker file is gone, whichever comes first, with
  the reason in its .lost; put a tighter bound in the command itself (`timeout`).
  Every `ttp detach` job gets, before it starts, a work dir of its own under the project's state
  (`$TTP_JOB_DIR`, in `state/detach-tmp/`) and its own short temp dir (`TMPDIR`, `TMP` and `TEMP`, a
  fresh dir under `/tmp/ttp-<uid>/` that the job's record in `state/detach-tmp/` owns, so socket paths
  stay short; the record itself where that root is unusable), neither inside the starting run's or
  check's scratch: the job never inherits the caller's `TMPDIR`. Both stay until the job ends, then
  go (the daemon removes those of a job killed without that cleanup); a push that exits busy (75)
  or a checks run that removes its scratch never touches them. A job a check detaches keeps its
  files there, never in the push's check scratch or the check's temp folder: those go with the
  check, maybe before the daemon starts the job. Stage what it needs from the commit inside the job
  (`git archive <sha>` from the repository, not the check's clean checkout). Make its work dir
  inside the job, after it is detached (`W=$(mktemp -d "$TMPDIR/gate.XXXXXX")` or under
  `$TTP_JOB_DIR`): one made before, under the caller's `TMPDIR`, goes with the caller. A check's
  queued job whose directory is gone when the daemon starts it starts in `$TTP_JOB_DIR`, with a
  line in its log saying so (a relaunched job's gone directory still fails it).
  A `ttp detach` run in a PID namespace that /proc does not show (as `unshare --pid` makes) is
  queued the same way, so the pid in a job's record (detached.json) is the host's, stored with that
  pid's start time: every pid check (stops, bounds, remote verdicts) takes a pid whose start time
  differs as gone, and liveness itself reads the job's lock, never a pid. A check that needs a job
  to outlive it uses `ttp detach`, never `setsid`, `nohup` or a `$$` pid file: those die with the
  check's sandbox, and the pid they record is the namespace's, which names an unrelated process (or
  none) on the host.
  `ttp detach --stop <name>` with no job recorded stops a driver
  the project's own launcher started: NAME.pid in the path's folder, $TTP_RUN_DIR or the task's t<id>
  folder, once its pid is verified (start time, user, project), gets a STOP file beside it, then SIGTERM and
  SIGKILL if it does not exit (0 stopped or already gone, 2 none found: it names where it looked).
  A launcher that lays its jobs out its own way (a job folder, no pid file) registers its stop in
  project.json: `"detach": {"launcher_stops": {"<glob of job folders>": "<stop command>"}}`. The glob
  matches the folder's path relative to the project root (`*` one level, `**` any number; an absolute
  glob the whole path); the command, a string split as a shell would or a list, never run by a shell,
  gets the job folder as its last argument and runs from the project root. `ttp detach --stop <job
  folder>` then runs it, logs the stop in the job-signal stream and as a `detached_job_stopped` event,
  waits for the job's processes (this user's, naming the folder as cwd, argument or environment value)
  to end and names any left: 0 stopped, 1 the command failed or processes are left, 3 refused (a folder
  outside the project root, the project's own folders, inside another tt-project project, or with a
  process of another project: nothing runs). `ttp detach --remote <ssh alias> [--dir <dir>] <name> -- <command>`
  starts the driver on another host with setsid nohup (default folder `~/.ttp-detach/<run>`, with its
  .log, .rc, .pid, .boot and .start); its probe `ttp detach --check --host <alias> <dir>/<name>` exits
  0 once the .rc holds 0, 4 once it holds another exit code or the driver is gone (pid dead, or that
  host's boot id changed), 1 while it runs and 255 while ssh fails. Started inside a run, its probe
  carries `--mirror <run folder>`: each answer copies the job's exit code and the last 64 KiB of its log
  back as `<name>.remote.rc`, `<name>.remote.log` and `<name>.remote.json` (the tail is sent only when the
  log grew), and an exclusive run's hold keeper refreshes them as it probes. Once an end was mirrored, a
  host that stops answering (it reset, or the network went) still yields 0 or 4, not 255. A driver gone without an exit code
  reads as died with the host reboot (the boot id recorded at launch changed), crashed or killed (same
  boot) or unknown (no boot id readable); a local job's check says the same. Long device runs append
  and fsync each step's result as the step ends, not only in an end-of-run summary file; with `--rows <file> --after <n>` it
  exits 0 also once that file (on that host) has more than n lines, and 0 once the job ended or its driver
  is gone, else 1. A retry_when that tests only an end marker over ssh is flagged. `ttp ssh-preflight <host>` exits 0 once the host answers over ssh
  and 255 while not (12 with `--classified`), 10 when its host key does not verify, 11 when
  authentication fails and 13 when this process cannot gain privileges (no_new_privs, a sandboxed run)
  and the host's ProxyCommand runs sudo, which ssh reports as `kex_exchange_identification` (ssh runs
  with strict host-key checking and no connection sharing);
  `ttp ssh-preflight @<resource>` looks the host up in config `device.hosts`
  ({resource: host}, else `device.runners.<name>.host`) each time it runs, so a waiting probe follows
  a resource moved to another box (an unknown resource exits 2). Config `device.runners.<name>` (opt-in) sets up one serial
  device-job runner per device host instead of a driver per task: `host` (ssh alias; empty = this
  machine), `dir` (its state folder there), `health` (a command that must pass before each job),
  `drop_check` (a command that tells a device drop from a plain failure), `max_drops` (default 2),
  `health_wait_s`, `job_timeout_s`, `reservation_cap_s` and `idle_exit_s`. With `reservation_cap_s`
  (the box's longest allowed device reservation) submit refuses a job whose limit is above it, a job
  queued before the cap was lowered is failed without being started, and a job with no limit of its
  own runs under the cap. `ttp devq` installs the
  runner on the host under a temporary name and renames it into place, never over a running copy; a
  runner stopped with TERM ends its health or drop check with it (jobs keep running and the next
  runner adopts them). A task queues each device job with
  `ttp devq submit <runner> --id <id> [--config <key>] [--timeout <s>] -- <command>` and waits on
  `ttp devq probe <runner> <id>`, which exits 0 once the job's done marker is written (done, failed,
  skipped or wedged, with exit code and log), 1 while it waits and 3 while it waits with no runner alive:
  the project's daemon then runs `ttp devq start <runner>` itself, without a model, at most every
  10 min and `device.runner_restarts_per_day` (default 6) times a day per runner, and queues one
  harness task to fix the runner after two failed restarts in a row or the day's cap. The runner is
  started under a lock (a second start does nothing), runs jobs in arrival order, runs a job again
  after a drop or a host reboot, and skips a config that dropped `max_drops` times in a row until
  `ttp devq clear <runner> <config>`; drops go to its `drops.log`. `ttp devq status` shows the queue.
  While a job runs, the runner reads its output for `wedge_patterns` (regexes; the default list holds
  the fatal hang signatures tt-metal and tt-umd print, such as "potential hang detected", "the device
  is unrecoverable", "the board should be reset" and "Timeout waiting for ARC msg request queue"). On
  the first match it stops the job (TERM, then KILL after 5 s), frees the device for the next job and
  writes the marker with status `wedged`, the pattern and the line, so the task can recover the device
  instead of waiting out the time limit. A job's `--wedge-pattern <regex>` (repeatable) replaces the
  list for that job and `--no-wedge-check` turns it off.
  Every job carries a build stamp, taken on the runner's host from the job's `--workdir`: the repo
  (origin without credentials), branch, commit, a dirty flag with a hash of the uncommitted changes,
  and the built artifact (`--artifact`, else the runner's `build_artifact`) with its size, time and
  sha256. Submit prints it and warns on a dirty tree or on changed C/C++ sources newer than the
  artifact; the job record and the done marker keep it under `build`, the runner stamps again at
  start and records any difference under `build_changed`, and the probe prints it.
  Before it queues a job, `ttp devq submit` lints its command so a job that would fail at once never
  takes a slot: it refuses a command `bash -n` rejects and a script the command calls that is missing
  on the runner's host or not valid shell there (checked over ssh in batch mode, read-only; an
  unreachable host only warns), and warns when a script path, the job id or the workdir names a task
  other than the submitting one. `--no-lint` skips the check.
  An opt-in job guard, run by the host at submit, keeps a job from harming the host (each part is off
  until set; `--no-lint` does not skip it): `script_lint` (`"warn"` or `"refuse"`) checks the command
  and the shell scripts it calls for process-group isolation (`setsid`, plus traps on EXIT, TERM and
  INT that kill the process group) and for a `df` or `du` guard before downloads or cache writes;
  `netfs_prefixes` (network-filesystem mount prefixes, none by default) flags weight and cache paths
  that resolve onto one, symlinks followed without touching anything under the prefixes
  (`netfs_policy` `"warn"` or `"refuse"`); `disk_max_pct` refuses a submit, and holds queued jobs
  like a failing health check, while a disk in `disk_paths` (default `/` and the runner's folder) is
  fuller. A job may declare `--input <path>` (repeatable; must be readable on the host) and
  `--cold-start <s>` (a warning when it is longer than the job's limit).
  `outside_user` (a regex for the command line of an outside device user to wait for, such as a
  script that drives the device outside the runner) closes the race between the two: while one of
  the user's processes matches it, the runner does not start or start a job, and the probe keeps a
  task with a pending job waiting until it ends.
  Config `runner.device_timeout_max_s` (opt-in) caps device-job timeouts: `ttp devq submit` refuses a
  longer `--timeout` and gives a job without one the ceiling when the runner's `job_timeout_s` is
  unset or above it; each job sees its limit (also under a runner's `reservation_cap_s`) as
  `TTP_DEVQ_TIMEOUT_S`, and workers are told to keep
  every device timeout (broker jobs, driver steps) within the ceiling and split longer runs.
  Locks and pauses are per project; a
  resource that several projects on one machine use is declared shared (`shared_resources` in the
  config, or `ttp machines add <alias> --shared [names]`), and then all of them take turns on its
  slots, a pause of it holds in each, and `ttp status` shows which project holds or paused it.
  Each project still gives its own slot count (`resources`); when they differ, every project
  uses the smallest, and `ttp status` and the coordinator say so. A count lowered while a holder
  keeps a higher slot applies to the next holders. A pause outlives the resource leaving the
  share: `ttp machines` refuses to unshare or remove a paused one, and a project that drops it
  from `shared_resources` keeps the pause as its own.
  To stop device work on a whole machine for every project on the host, hold it once:
  `ttp pause <name> --machine <alias>` (or the coordinator's `resource_pause` with `machine`)
  stores one hold in `~/.tt-project/fleet-holds.json`, and every project pauses each resource
  it uses on that machine (the alias, `<alias>-...` names, its hostname) with no pause of its
  own. A pause asked for a resource the hold covers changes nothing. A hold you set from the
  shell or ask a coordinator for is yours, and only your word lifts it (`ttp resume <name>
  --machine <alias>`, or a message to any project's coordinator). The setter's daemon checks
  its end, or the machine's recovery owner when the setter cannot act.
- Each task edits only its own worktree. A code task branches from the first of:
  `delivery.base_ref`; `delivery.push_branch`, then a branch the charter names as
  `branch <name>`, each only if it exists here or on origin; the remote's default branch
  (`origin/HEAD`); the checked-out branch.
  Other kinds run in the project root unless `worktree.kinds` lists them (say `["work"]`); they
  must not move its HEAD. A run that leaves the root on another branch or with new uncommitted
  changes raises an alert naming it. Before a worktree is made, a root left detached or off its
  branch (`worktree.root_branch`, else the one it was last seen on) is put back when it has no
  uncommitted changes, else alerted; worktrees never start from such a HEAD.
- Smoke before landing: `delivery.push_checks` gate every landing, so keep them under 10 min on
  their host (aim 5). Slower tests belong in `delivery.post_land_checks`, which run after landing:
  the daemon runs them detached on each new pushed sha, without a model. A failing test that passes on
  a rerun is flaky (quarantined, not reverted); one failing before the landing too is not its. A new
  failure is bisected over the landed changes to the guilty one, which is reverted forward on the
  push branch through the push queue (never forced), and one code task is queued to fix it on its
  failing tests alone. `ttp status` and the daily
  review show the landing check time (median and p90, last 24 h and last 10 runs); over
  `checks.smoke_max_s` (default 600 s) the project's coordinator gets one observation a day naming the
  slowest tests (push-checks reference, "Smoke before landing"). `ttp status` also shows the waits:
  checks wait to start and approval to landed (median and p90, last 24 h).
- Where reviewed changes go straight to a shared branch, `ttp push` publishes them guarded: it
  refuses uncommitted changes, rebases onto the latest tip, runs `delivery.push_checks` on the
  exact commit it pushes, starts over if the branch moved meanwhile, and never forces. A
  checked-out branch the remote already has (published with `ttp push --own`) is never rewritten:
  the rebase runs on a detached copy and the branch stays as it was. It exits 4
  when the rebased change edits a plugin under `plugins/<name>/` but a manifest keeps the version
  already on the branch (two batches that bumped to the same version). The target
  is `delivery.push_branch`, which must be set explicitly; it refuses without one, without
  checks unless the change touches only docs (`*.md`, `*.rst`, `docs/`, ...), when
  `delivery.push_allowed` is false, and for `HEAD`, `main`, `master` or the
  remote's default branch. Pushes to one branch take turns under a lock that a killed push or a
  reboot frees. One that waits longer than `delivery.push_wait_s` exits 75 and prints a
  `retry_when` for its hand-off: `ttp push --free`, which exits 0 once the turn is free. Unset,
  the wait is twice the last measured check run plus 60 s, at least 900 s and at most 2 h.
  Each check runs at most `delivery.push_checks_timeout_s` (unset: four times the last measured
  check run, at least 4 h; 0 for no limit): one past it is ended and the push fails, saying so.
  The limit counts run time: wall time since the check started, less time it spends waiting for
  a grant (a `ttp lock`, a checks lane or a host-wide checks slot) or for a test-pool slot. Run
  past it, the check is ended within a few seconds and fails, and the push log says it timed out,
  after how long and how much was pool wait. A check still waiting for a test-pool slot once its
  wall time less grant waits reaches the limit is ended as `busy, not failed` and the push exits
  75 (a full pool is not the change). A check still
  waiting for a grant once the limit plus the push wait has passed is ended as `queued, not run`
  and the push exits 75 (busy, try again), never a failed check. A gate of your own that is not
  behind `ttp lock` counts as run time.
- A check script that picks its own test-pool host (shards on a CPU pool, say) reports that wait
  so the push can show it: wrap a command that returns once a slot is free in
  `ttp pool-wait -- <cmd>`, or mark a wait loop of its own live with `ttp pool-wait --start`
  before it and `ttp pool-wait --stop` after it (same `--what`; from one script, or paired with
  `--id <name>` across processes; a script that ends first ends its wait). A pool that times a
  wait itself reports it once over with `ttp pool-wait --waited <seconds>`. While a check waits,
  the push log gives the seconds so far every minute and `ttp push --result` shows them; at the
  end the log gives the whole pool wait. Past `delivery.push_pool_wait_s` of pool wait (default
  4 h, 0 no bound) the check is ended, saying how long it waited; no test ran, so it is an
  infrastructure fault (below), never a failed check.
- A check that fails before any test ran is the host or its pool, not the change: its output
  names a work or head dir gone before the copy (`cd: <absolute dir outside the repository>: No
  such file or directory`), an empty or corrupt archive (tar's `This does not look like a tar
  archive` or `Unexpected EOF in archive`, gzip's `not in gzip format`, `rsync error:`), its own
  `no verdict` (`FAIL <shard> (infra); no verdict`) or no free pool slot (`no free slot`, `no <name>
  pool free`), and no test verdict (a pytest count or result line, TAP `ok`/`not ok`, an upper-case
  `PASS` or `FAIL` on a line not saying `no verdict`, a traceback). `ttp push`, `ttp land`,
  `ttp checks` and the push queue run it again up to 2 times (no free pool slot as the last fault
  seen: not at once; a pool wait that later lost its head dir reruns);
  still so, the push log says `infrastructure fault: ... no test ran`, `ttp push` exits 75 with a
  `retry_when`, `ttp checks` exits 75 with `infra_fault: no-test-ran` in checks.json, and the push
  queue holds the batch (`setup_fault`) and tries it again after 10 min; held this way 3 times in a
  row on one check (`delivery.push_setup_fault_escalate_after`, 0 = off), one high
  `push_setup_fault` alert names the check, the fault lines and the batches, and clears once that
  check passes or a batch pushes. Until it clears, that check's next setup fault on a batch head
  is a failed check of that head instead of a hold: the batch's entry search sends back the entry
  that brings it and pushes the rest, so one entry whose check names a missing dir or setup outside
  the repository cannot hold every other approval. One on the tip alone (every head fails so) or no
  free pool slot still holds the batch and blames no entry. It never counts as passed.
  Any test verdict in the output keeps a failure a failure. A gate of your own that knows it ran
  nothing can say so directly: exit 75. A job a check detaches makes its work dir inside the job,
  after it is detached, so it outlives the check (see `ttp detach` above).
  A check script that runs its own test pool and is granted fewer slots than it planned asks
  `ttp pool-admit --planned N --granted K [--files F] [--what <pool>]` before it starts the shards:
  exit 0 starts them; exit 3 means the run on K slots is estimated to go past the time the check
  has left, so it is not started there, and stdout gives the fewest slots that would fit. The
  script then gives the slots back and waits for that many inside `ttp pool-wait` (that wait
  is not run time, but a check still waiting at its limit is ended busy, so it asks again once granted), or runs the set on this host's checks
  slots (`ttp checks-shard`). The estimate is the median slot-seconds per file (with `--files`),
  else per planned shard, of earlier passing runs the script recorded with `ttp pool-admit --ran
  <s> --granted K [--planned N] [--files F]`; with none, each planned shard is taken as half of
  the whole limit. Granted every planned slot, or with no limit known, nothing changes. A push
  check gets its limit in `TTP_CHECK_LIMIT` (the time left takes off its grant and pool waits, as
  the push does), a `ttp checks` run its wall cap in `TTP_CHECKS_WALL`.
  Why this design: refusing the cut run is the simplest rule that never starts a run certain to
  time out, and it leaves the project's own pool in charge of where its shards go. The limit is
  never scaled for a cut run: that would let one run hold a push longer than the project set.
  A gate that runs its test files on a pool of hosts starts each shard as `ttp pool-shard --shard <id>
  --pool <alias> [--pool <alias>...] [--cancel <cmd>] -- <cmd...>` (`{pool}` and `{shard}` in the
  command are filled in; TTP_POOL and TTP_POOL_SHARD are set). The shard prints one line per file
  done (`PASS`/`FAIL` at the start, or pytest's `[ NN%]` progress; `--done-re` changes it). Its time
  before the first file (a queued batch job, setup) is a test-pool wait: the push log shows it and
  `delivery.push_pool_wait_s` bounds it, and like any pool wait it is not run time against the check's limit. A shard with
  no file done for its stall window is ended with its process group, `--cancel` cancels its batch
  job, the stall goes to `~/.tt-project/check-slots/pool-stalls.log` with the shard id and pool alias, and
  its files run again on the next `--pool` given; when every pool stalls it exits 124. The window is
  `checks.pool_shard_stall_s` (account setting) when set, else four times the median seconds per file
  measured on this machine, at least `checks.pool_shard_stall_floor_s` (default 600 s). A stalled
  pool is skipped by later shards for 30 min or until a file is done there; when every pool given
  is marked, the one marked longest ago is tried.
- A check is a command, which runs on every head. One that only makes sense once a file exists
  (a test file a later change adds) can opt in to being skipped where it does not apply:
  `{"run": "pytest tests/test_new.py", "if_exists": "tests/test_new.py"}`. `if_exists` is a
  repo path or glob (a directory counts by its files) looked up in the commit being checked. A
  skipped check is logged as `skipped (not applicable: ...)` by `ttp push`, the push queue and
  `ttp checks` (in `checks.log`), and never counts as passed: when every check is skipped the
  push or `ttp checks` fails. Plain string checks never skip; `after_push` takes the same form.
- A check that covers only some paths can be scoped by them:
  `{"run": "pytest -q tests", "if_changed": ["src/**", "tests/**"]}` (one glob or a list, matched
  as `delivery.push_exclude_paths` is). It is skipped, and logged as `skipped (not applicable:
  ... changes nothing under ...)`, when the diff from the push target to the head being checked
  (`git diff --no-renames <target>...<head>`, so a moved file counts under both names) touches no
  matching path, and runs normally otherwise. The push target is the fetched tip for `ttp push`,
  `ttp push --own` and the push queue, the pushed batch's previous tip for `after_push`, and the
  local copy of `delivery.push_branch` for `ttp checks`; with none to compare with, the check
  runs. A diff git cannot give (a missing ref, no merge base) fails the check. A head on which
  every check is skipped this way touches nothing any check covers, so it goes unchecked, as a
  docs-only change does without checks: a notes-only branch passes `ttp push --own` without a
  catch-all check. If any of the skips came from `if_exists` instead, nothing checked the head and
  it fails as above. A check may set both keys; outside its declared scope it skips, but inside
  that scope a missing `if_exists` target fails rather than leaving the change unchecked.
- A pass carries across a rebase without running again, decided by a fixed rule with no model and
  no user step. When `ttp push` or the push queue rebases a change onto a tip that moved since its
  checks passed, a check's pass on the old head counts for the new head if the change's own diff is
  unchanged (same patch id, no conflict-resolution edits, the two heads differing only outside what
  the check covers) and every file the new upstream commits changed lies outside what the check
  covers. A check covers its `if_changed` globs if it has them, otherwise every path, and never a
  path matched by `delivery.push_exclude_paths` or by its own `ignore_paths` (one glob or a list,
  matched as `if_changed` is): `{"run": "pytest -q tests", "ignore_paths": ["docs/**"]}`. The push
  output and the push record (the `carried` list of the push outcome; `checks.carried` in a push
  queue batch marker) say `verdict carried from <sha>; upstream delta touched only: <paths>`. A
  delta touching a covered path reruns the check as usual. `ignore_paths` only decides this reuse:
  it never narrows what a check covers or whether it runs on a fresh head, so setting it is not a
  rescoping and a coordinator may set it itself. Passes the push or the push queue made carry; of
  the passes `ttp checks` records, only a task's own full candidate run of the push checks carries,
  to that change's head in a push queue batch (judged without the batch's own version bump, which
  touches only the version files and a changeset), with `(the task's own ttp checks run <id>)`
  added to the line. A targeted run, a single command's pass or a batch of several changes does not.
- Config warnings flag checks that pin a full commit SHA, use an absolute temporary environment,
  or name a target missing from the branch tip without `if_exists`. Declare applicability with
  `if_exists`/`if_changed` and retain coverage for the code the check protects. Warnings do not skip
  commands. `ttp checks` records a missing-target failure only when the target is absent from the
  tested Git tree (Python cannot open it, pytest cannot find it, or collects no tests). A check-only
  review failure on an unrelated change becomes a wait with one harness repair task per check
  signature, rather than blocking its dependents. Changing `delivery.push_checks` reruns those
  checks without a model. Related changes still fail; missing scope or unreadable history is treated
  conservatively. Without explicit scope, a nested component test covers its component and root
  `tests/` covers all code. A retry never approves code unless the reviewer supplied an `on_pass`
  verdict and the unchanged, clean commit passes its checks. A mandatory selector with no case
  for HEAD can report `check selector: no case for HEAD <40-hex>` (or `no matching case`). A
  nonzero check with that exact tested commit gets the same single repair owner and waiting
  dependents. Its repair retains the selector and every mandatory check; a previous head's pass
  or byte-equivalent tree never approves the unmatched head.
- Checks run with `TTP_PUSH_MODE` set to `target` (`ttp push` or the push queue, to
  `delivery.push_branch`), `own` (`ttp push --own`) or `checks` (`ttp checks`), and `TTP_PUSH_TIP`
  set to the tip of the branch pushed to as fetched before the checks (empty for a new branch and
  for `ttp checks`). `TTP_PUSH_BRANCH` names that branch as the remote calls it, short (no
  `refs/heads/`): the push branch for `ttp push` and the push queue, the task's own branch for
  `ttp push --own`, the local branch for `ttp land`, empty for `ttp checks`; a check that refuses
  content by destination reads it, not the branch checked out. A check that should only gate the push branch tests
  `[ "$TTP_PUSH_MODE" != target ] || ...`. `ttp push --own` may instead reuse a
  `ttp checks` pass recorded on exactly HEAD's tree (run with mode `checks`, an empty tip and an
  empty `TTP_PUSH_BRANCH`), so logic that only runs in mode `own`, or that depends on
  `TTP_PUSH_TIP` or `TTP_PUSH_BRANCH`, cannot count on running there; the push queue (`target`)
  always runs its checks. In the other direction, `ttp checks` may
  reuse a push-queue pass of a plain pytest check for the same tree, command and environment:
  that pass ran in mode `target` with that batch's tip, not `checks` with an empty tip.
  `env_id` does not cover CHECK_ENV_VARS (`PYTEST_ADDOPTS` and similar), so a reused
  push-queue pass ran under the daemon's values of those variables.
- Local leak patterns: when the account has a leak-pattern file (`TTP_LEAK_PATTERNS_FILE`, else
  `$XDG_CONFIG_HOME/tt-project/leak-patterns.txt`, `~/.config/tt-project/leak-patterns.txt`, or
  that file under the account's home from the password database when HOME is a temp one), local
  checks and `ttp sandbox -- <cmd>` get `TTP_LEAK_PATTERNS_FILE` set to its absolute path, so a
  test that runs under a temp HOME (this repository's suite gives every test one) still reads it.
  Only the path is passed: the file is never printed, logged, copied into a worktree or sent to
  another host, so a check on another host (`host`, Slurm) skips such a test. To gate pushes on it
  wherever the other checks run, add a check pinned to this host (`"host": "local"`) that runs only
  that test. A pass's `env_id` holds a hash of the file (never its text): a pass from before the
  file existed or changed is not reused.
- A waiting hand-off on the run's own detached checks (`retry_when` `ttp checks --result <run dir>`)
  wakes a model only on a failure, a broken probe or a timeout. With an `on_pass` the pass records it;
  a plain code task with a summary and nothing left records `done` (the default on_pass). Any other
  such hand-off that names no `on_pass` or `on_pass_cmd` and asks for nothing after the pass (no
  `next_step`, PR, `wake_tier` other than light, review verdict, or PR or landing in its spec) gets the
  on_pass_cmd `ttp checks --settle <run dir> --head <sha>` filled in, with the worktree's clean HEAD
  when it handed off (log line `hand-off: its checks wait got the on_pass_cmd ... filled in`; one that
  asks for more is logged `kept` and wakes on the pass). `ttp checks --settle` writes the hand-off's
  `on_pass` or, without one, a `done` hand-off from its summary (artifacts, metrics, self_check,
  resolves and similar fields carried over) to `$TTP_RESULT` only when checks.rc is 0, checks.json
  records a passed candidate run on that head, the worktree still has it checked out clean, and the
  run's result.json is the waiting hand-off on this wait with no `next_step`; otherwise it writes
  nothing and exits 1, so the task wakes. A pass on another head never records anything. A worker can
  name it itself as its `on_pass_cmd`. A hand-written on_pass_cmd tests for a pass with
  `ttp checks --result <run dir> --passed`: exit 0 only once the checks finished and passed, 1 when
  they failed, died or still run (plain `--result` exits 0 on a failure too, as a wait must), e.g.
  `ttp checks --result <run dir> --passed && <command writing the hand-off to $TTP_RESULT>`.
- `ttp checks` runs, and the checks of `ttp push` and the push queue, share a host-wide cap, across
  every project on the machine: each takes one of
  a few slots under `~/.tt-project/check-slots` before its commands run, and a run over the cap
  waits its turn in arrival order. The wait shows in `ttp status`, the task's notes and the
  `--result` probe; it never fails the checks. A foreground `ttp checks` (a tool call) waits a
  minute at most: then it starts the same checks detached, in the same place in the queue, prints
  their `retry_when` and exits 75 (with no project it waits on in the foreground).
  Slots are OS file locks, so a crash or reboot frees them. Workers' sandboxes may write the slot
  directory; where it cannot be written, the checks stop with an error naming it (exit 2; a push
  does not go) instead of running uncapped. Unset, the cap follows the load: as many checks as the
  cores left over the 1-minute load average hold at an assumed load of 4 each, at least one slot
  per 16 cores (and at least one), at most one per 8. Running checks count in that load, so the cap
  shrinks as they load the host; a running check keeps its slot. Whatever the cap (unless 0), a check starts only
  while the host's free cores now (busy cores read from /proc/stat over a few seconds) cover one check's
  measured cores; with no check running one always starts. Its test grant is then at most those free
  cores (at least 2); `checks.live_cores 0` turns both off. Nor does a new check start here while the
  host's disk is saturated by writes, even with none running: while `Dirty:` in /proc/meminfo is over
  `checks.dirty_max_mb` (default 2048) or `full avg60` in /proc/pressure/io is over the I/O pressure limit
  `checks.io_full_max` percent (default 10; a kernel without PSI skips only this signal). Both are account settings
  (`ttp config --account checks.dirty_max_mb N`, or `TTP_CHECKS_DIRTY_MAX_MB` / `TTP_CHECKS_IO_FULL_MAX`;
  0 turns that signal off). A run that may spill then tries the spill hosts first, else it keeps its
  place and waits; running checks and the push branch's checks (its priority slot) go on. `ttp checks --slot-status` shows the values
  and the reason. A set cap wins: `ttp config
  --account checks.host_max N` (0 turns the cap off) or `TTP_CHECKS_HOST_MAX`; it is one value for every
  project (a project's own copy is ignored and `ttp config checks.host_max` shows the account's). A run with
  no task takes the task and project of the project run it was started under; one with none queues behind
  every project's waiter and, while one waits, such runs hold at most `checks.bare_slots` slots (default 1).
  `ttp checks status` flags a holder that has held its slot `checks.idle_hold_s` (default 600) with no test
  using CPU and names `ttp checks-yield`; it stops nothing. While
  the 1-minute load average is under half the cores, up to `checks.load_extra` more slots open (default 2,
  0 turns them off); check commands (`ttp checks`, push checks, pool shards, and on a remote or spill check host too, where it has the tools) run under `nice -n 19`/`ionice -c3` (idle IO), while the daemon, coordinator and workers keep their normal priority; `checks.nice` false, per project (`ttp config <project> checks.nice false`) or account, runs them at normal priority (`checks.nice` N, 0-19, sets the level; `checks.ionice` `idle` or `best-effort[:0-7]`; both per project or account). Each check
  run also takes test-worker slots from a host pool shared by all projects (testslots: default half the cores, at
  least 2; at most min(8, half the pool) per run; `checks.test_pool`, `checks.test_run_max`, 0 pool turns it off;
  a project may only lower them; a full `ttp checks` run waits until `checks.test_run_min` are free, default
  max(2, half a run's grant), and a repository opens no more checks lanes than the pool fills at a full grant) and passes its grant as PYTEST_XDIST_AUTO_NUM_WORKERS and TTP_TEST_SLOTS (a
  suite's `-n auto` uses the whole grant). A project's test-worker budget is its checks at once
  (`checks.test_project_max`, below) times one run's grant, at most the pool, so every check under that cap gets its
  workers (3 checks at once with a grant of 8 make a budget of 24: each run gets 8, never the whole budget); `checks.test_project_slots` (chat-settable, a whole number of at least 1) sets the budget outright.
  Both stay within `checks.test_project_workers` (chat-settable, per project or account; default the smaller of 8 and
  one per 8 cores, at least 2, raised to `checks.host_max` x `checks.test_run_min` within the test pool so every run the
  host-wide cap admits can start, plus the cores the runnable CPU demand (as for the load guard, below), less one per test slot the project itself holds, leaves under
  0.75 x the cores, at most half the cores, so its own suites never shrink it; `checks.load_extra` 0 keeps the base; a shrink bounds new grants only), the most test workers one project's suites run at once, which also bounds one run's
  grant. No new suite starts while the host's runnable CPU demand is above its cores (the CPU limit,
  `checks.load_guard`, a factor of the cores, default 1; 0 turns it off) at every reading over the last
  `checks.load_sustain_s` (default 60 s; readings of all runs on the host, check-slots/cpu-demand.jsonl; 0 reads
  the demand now alone), so a burst of short-lived git, ssh or python processes holds nothing, or while macOS
  reports memory pressure at warn or critical: it waits and its lane's wait note says why; running suites go on. The demand is the busy cores, or the tasks running or waiting to run where more
  (/proc/stat over a few seconds; the 1-minute load average where that cannot be read). Unlike the load average
  it leaves out tasks waiting on I/O (D state), so a contended disk with idle cores holds nothing here: disk
  contention has its own I/O pressure limit (`checks.io_full_max`, above). `ttp checks status` shows both
  readings, both limits and which one holds. Four
  passes still let one suite each start past it: one for an urgent run (priority 1 or better), one for an urgent
  or push run, one for a run owed to the user (priority 0), and a floor pass for any run, so two urgent runs never
  queue behind each other and load from work outside the queue never stops it while slots are free. A fixed
  count of passes, not urgent runs sharing one, keeps the guard's bound on an overloaded host. An urgent run the
  guard holds 10+ minutes raises an `urgent_check_wait` event naming the load and its passes' holders. The floor goes to the most urgent, oldest run the load alone holds; a run parked for another reason
  keeps no place in that line. A run takes its pass the moment its gate lets it start and keeps it until its
  test slots are granted, so only one parked run leaves the park per pass and none holds a host-wide checks
  slot only to wait under the guard. Push
  checks wait there at most `checks.push_load_wait_s` seconds (default 300; `off`: no bound), then start on
  their branch's reserved lane alone, one per lane at a time. Heavy suites
  belong on another host where a project has one allowed (`delivery.checks_host`).
  A check on another host gets its commit through a bare mirror of the repository kept in that host's
  tt-project folder (`check-mirrors/`, beside `checks-env/`): each check streams a git bundle of only the
  commits the mirror lacks over ssh (KBs once the mirror is seeded; a new mirror is seeded in steps, and a
  check cut short resumes the seeding), checks it out into a fresh folder and removes the folder and its
  ref afterwards. A new mirror is first filled from a clone already on that host when
  `delivery.checks_seed` names one (`{"<host>": "<absolute path>"}`; that clone is only read), so only the
  commits after the newest one both share cross the link. No tree is sent from here: a host without usable
  git is the host's fault, unless `TTP_REMOTE_TREE_COPY=1` sends it the tree over a compressed ssh. A copy prints its
  progress every 30 s (`TTP_REMOTE_PROGRESS_S`); one past its limit (`TTP_REMOTE_COPY_LIMIT_S`, default
  540 s) or with nothing moving for 300 s (`TTP_REMOTE_COPY_STALL_S`) is the host's fault: the check waits
  or runs elsewhere, never failing the change.
  Checks that run on another host (a check's own `"host"`, `delivery.checks_host` or a spill host), with at most
  light checks here, use none of this host's CPU: push checks and `ttp checks` take no host-wide checks slot or
  test slots here and never wait for this host's guard. They wait instead while their host's runnable demand,
  read over ssh from its `/proc/stat` (cached 30 s, read fresh just before a start), with one run's cores for
  each run this machine started there in the last 90 s (the load shows them late), is at or above 0.8 x its
  cores (`checks.load_guard` x if lower), and say which host holds them, again every `checks.push_load_wait_s`;
  they never start there while it is, however long that takes. An unreadable host never holds them.
  Waiters are served first-come: one whose project is at its budget lets other projects go first but keeps its
  place in its project's line, where the most urgent task priority goes first, then arrival, so that waiter gets
  the next freed slots. A priority-1 check whose project's budget is held only by less urgent runs takes one extra
  run's grant from the pool's free slots instead of waiting, and stops none; push checks take a reserved test slot of their branch when the pool is full, and a
  project runs at most `checks.slots` full `ttp checks` at a time per git repository (its lanes; worktrees of one
  repository share them; other repositories and projects never wait). A run whose commands go behind
  `checks.wrap_cmd` takes that wrapper lock's own single lane instead, so runs behind one wrapper lock go one at
  a time and lock-free checks run beside them; it takes it before the wrapper starts, or with `checks.wrap_lane
  after` (for a wrapper that waits for its lock) only once the wrapper holds its lock, so a run queued there holds no lane (that lane is not held to
  `checks.test_project_max`: waiting for the cap inside the wrapper would hold the wrapper's lock meanwhile). A change of `checks.wrap_lane` reaches `ttp checks` runs that start after it; a run already queued for the wrapper lane or holding it keeps the mode it started with until it ends (a waiting run that re-executes for a runtime upgrade uses the new setting). At most `checks.test_project_max` of a project's checks run at once
  over all its lanes (chat-settable, a whole number of at least 1 or `auto`; unset or `auto`, the runs the test-slot pool fits
  side by side, at least `checks.slots`; a project's `auto` keeps an account-level number off it, and a waiter held by a
  fixed number under the host's size says where it is set and what the host fits); a run counts against it
  only once its test workers are granted, so one still waiting for them never holds up the others; waiting
  checks start in task-priority order across them, and a priority-1 check whose lanes are held only by less urgent
  runs starts beside them in an extra lane while the load is under half the cores, never in a worktree that already
  runs checks. When `checks.test_project_max` is full of less urgent runs, that priority-1 check still starts at once
  in an extra lane past the cap, one such lane per project at a time; it never stops a running check. A full
  `ttp checks` run of an urgent or priority-1 task skips that wait: it takes the extra lane when it can, else a
  lane at once, and queues for a host-wide checks slot (where it preempts routine runs), never behind runs that
  only queue there, and still
  runs only within these bounds unless it holds a reserved urgent slot. A waiting run reads `checks.slots` again
  at each poll. Unset, `checks.slots` (chat-settable, a whole number; `checks.suite_concurrency` is its chat-settable
  alias) is the number of runs the host's test-slot pool fits side by side, at least 2 and at most 4, and lower only
  when available memory is under 8 GiB per lane. Runs at once do not collide: each has its own clean checkout,
  TMPDIR, output folder and block of ports (`TTP_PORT_BASE`, `TTP_PORT_COUNT`, for suites that bind fixed ports);
  checks that run in the authoring worktree itself also hold that worktree's one lane. On Linux a run's clean checkout
  and TMPDIR go on the RAM disk (`/dev/shm`) when it has room for this run's scratch (the checkout's real size,
  submodules included, times 1.5 plus 2 GiB of temp files, at most a quarter of the disk; 4 GiB counted per run
  already there) while keeping a quarter of it free and a fifth of the RAM available, else under `/tmp`, as are
  checks that build (cmake, make, ninja): concurrent suites writing their temp git repositories to a disk filesystem wait on its locks.
  A run there counts only while its check is live (its dir holds a mark naming its check's allocation, or was made
  in the last 5 minutes), so leftover dirs a stray process recreated never push later runs onto the disk. The run's
  TMPDIR is pytest's temp root too; the checks runner removes the dir when the run ends, and recovery removes a
  killed run's. The suite's own test git repositories skip fsync (git 2.36+, through the test session's git config). A lane goes to the most urgent
  waiting task priority first (read from the task's record; a check with no task, or a task with no priority,
  ranks as p3), then arrival; a running holder is never pre-empted, and a waiter gains one priority step per
  `checks.priority_age_s` waited (default 900, 15 min; project, then account), never past p1, so none starves.
  Among equal ranks a native priority goes first (a native p1 before a p3 aged to p1), until a waiter has waited
  four aging steps (1 h by default): from then on arrival decides, so a stream of p1s cannot starve it.
  `ttp checks`, `ttp checks status` and `ttp status` show each waiter's priority and its rank now (`p4 ranked p2
  (aged)`). Light checks never wait: a run whose every command is a
  syntax, lint, format or validation check (`python -m py_compile`, `ruff check`, `black --check`,
  `git diff --check`, `shellcheck`, `bash -n`, ...; the words of `checks.light_cmds` add more; `ttp checks
  --light -- <cmds>` declares its own) runs at once outside every slot, and still fails when it fails. A lane
  whose holder's process is gone or whose heartbeat stopped is reclaimed and logged. A waiter that has waited
  `checks.max_wait_s` (default 300, 0 turns it off) and is cheaper than every holder (by recorded run time,
  else by kind) takes an extra lane beside them. A task's targeted checks (only the tests its diff reaches)
  are priced by that share of the full checks' run time and shown as `targeted: N tests`. A waiter says who holds the lanes and its place in the queue
  each minute; `ttp status` and the coordinator's digest list waits over 10 min, and the daemon records one
  `checks_slot_wait` event per wait past `checks.wait_event_s` (default 900, 0 turns it off). Only one of them
  wakes the coordinator per queue incident (from the first waiter past that threshold until none is); the
  rest are recorded `coalesced` and show in the digest and `ttp status`. The incident wakes it again only when
  a decision is due: its longest wait passes `checks.wait_escalate_s` (default 7200, then once per as long
  again; 0 never), a holder has run `checks.wait_hang_factor` (default 3, 0 never) times the median
  passing run of its own command list (a full suite is never timed against targeted runs; a label with no
  such run yet is not timed; a likely hang only once its log stopped growing, else slow), or a holder's task has ended (a review precheck: once no review reading it is open). An `observation_mute` on source
  `daemon:checks_slot_wait` covers these events too (recorded `muted`); coalescing already keeps a long queue
  to one wake, so a mute is only for a queue the coordinator has already decided to leave alone.
  `delivery.checks_spill_hosts` (an ordered list of ssh aliases, default empty: checks never leave this machine)
  lets a run that may spill try those hosts before it takes a place here (`delivery.checks_spill_first`, default
  true; false: only after waiting `delivery.checks_spill_after_s`, default 60 s, here) and run, with the test
  workers that host's idle cores give it (cores less its 1-minute load), on the emptiest host (its load plus
  this machine's runs there, over its cores; no fixed per-host cap) the
  charter names, the machines list holds, no pause covers and whose `ttp lock <alias>` nobody else holds (the run
  holds it shared while it runs), where no device job runs, that ssh reaches, whose 1-minute load (with this
  machine's runs started there in the last 90 s) is under 0.8 x its cores and that has a free slot; others
  are skipped with the reason and the run keeps its place here. A host whose probe fails backs off (60 s,
  doubling to 15 min, for every project on the machine) and is tried last until then. On a workstation or laptop,
  `delivery.checks_remote_only` (default `auto`: on there; `true`/`false` overrides) makes a heavy run wait
  for a busy spill host instead of taking a place here; it runs here, niced, only when every host is out of
  reach. Light checks (plugin validate, quick lints) stay here. A check that runs an agent CLI (`claude`, `codex`,
  `cursor-agent`, also inside `sh -c`), such as `claude plugin validate --strict`, or says `"host": "local"`, stays
  on this machine while the run's other checks (pytest) spill, and the run gives one verdict for both parts. A
  failure of the part kept here counts at once. Only a check that is not a test suite or build splits off this
  way, so a spill host needs git, tar and what its checks run (python3 and the checks env for pytest), never an
  agent CLI or its sign-in. A check whose script file calls one says `"host": "local"`. A device job means one of the host's device
  locks (`<alias>-device`, the device names of its machines-list `shared` entry, `device.locks` names on it)
  is held, reserved or waited for (a pool check run's own hold of the host's lock is none, even when `device.locks`
  lists the bare alias), or a broker heal check (preset broker) of the project on that machine
  reports a running job row; a broker status that cannot be read never keeps a run off. Runs already there go
  on, and `ttp status` and the web app show the skip in one line with the host and the reason.
  `checks.spill_beside_device` (ssh aliases, or `"*"` for every spill host; default none) names hosts where the
  project's own rules let its CPU-only checks run beside a device job: there the device job keeps no run off, a
  pause or a held machine lock still does, and the run and its `ttp status` line say it runs beside a device job
  (`ttp config <project> checks.spill_beside_device '["host-a"]'`). A spill host may be one host of a
  machines-list entry with several hosts, written `<alias>:<host>` (`"cluster-a:host-b"`): it is allowed when its
  machine is, a pause of the machine or the host keeps it off, the device gate and probe look at the host and the
  run holds the machine's lock; single-host entries work as before. `checks.spill_pre_cmd` (user-set, no default)
  is the project's own read-only pre-run hook, run here before a run spills to a host, with the host as its last
  argument and `TTP_SPILL_HOST`, `TTP_SPILL_MACHINE`, `TTP_PROJECT` set (30 s bound): exit 0 goes, exit 75 skips
  the host for now, any other exit is reported as a hook error and the host is skipped. It also runs on
  `checks.spill_beside_device` hosts, so there the hook decides; tt-project ships no neighbour, serving-state or
  site check (`ttp config <project> checks.spill_pre_cmd './tools/may-spill.sh'`). The account's pool, `checks.spill_hosts`
  (`ttp config --account checks.spill_hosts '["pool-a", "pool-b"]'`; only the user sets it, a run may only
  clear it with `[]`), follows each project's own list, so every project on the machine spills to it without a
  list of its own; its hosts need no charter line but must be in the machines list. A project's own `[]` keeps
  its runs here, pool included. Runs with a check host or the authoring tree never spill; runs behind
  `checks.wrap_cmd` spill only when the project sets `checks.wrap_needs_device: false` (they run there without
  the wrapper; unset or true, the run stays here, and its `checks.log` and queue-wait item say `no spill host can
  take these checks` because neither `checks.spill_wrap_cmd` nor `checks.wrap_needs_device: false` is set). Two settings drop the wrapper, for different cases.
  `checks.no_device_cmds` (or `ttp checks --no-device` for one run) names commands that never need a device or
  the wrapper's lock (lint, unit tests): they run without the wrapper here too, and a run of only such commands
  may spill (`ttp config <project> checks.no_device_cmds '["ruff", "pytest -q tests/unit"]'`).
  `checks.wrap_needs_device: false` (project-wide) is for a wrapper that only serializes this machine's CPU test
  runs: here every command still runs behind it, and a run that waits may spill and runs there without it
  (`ttp config <project> checks.wrap_needs_device false`); never set it for a wrapper that holds a device. Both
  set: the named commands skip the wrapper everywhere, the rest keep it here, and a run with any wrapped command
  spills only because of `checks.wrap_needs_device: false`. `checks.spill_wrap_cmd` (default none) is the wrapper
  checks run behind on a spill host (a relative path in the copy of the commit there, else a name on its PATH);
  a wrapped command with one may spill without `checks.wrap_needs_device: false`. Per check (`delivery.push_checks`
  entries too), `"spill": true` lets it spill even with `"host": "local"`, `"spill": false` keeps it here (left out:
  a check with host `local` stays here, others follow the pool rules), and `"spill_wrap_cmd"` overrides the
  project's remote wrapper. A remote wrapper missing or not executable on the host is a host fault: the check never
  runs unwrapped. A wrapper that refuses a run (say, a busy host) writes the file `$TTP_WRAP_REFUSED` names and
  exits 75: a host fault too, so the check runs again elsewhere; exit 75 without that file reads as a failed check. The order: a run asks the spill hosts first (with `delivery.checks_spill_first` false, only
  once the slots here are full: an urgent run at once, others after `delivery.checks_spill_after_s`); a run
  waiting here (its lanes, the host-wide checks cap or the load guard) asks again after each such bound; the first listed host
  that is allowed, free and reachable takes the run; only when every one is full, paused or unreachable does it
  wait here (and try them again after the same bound). A run that does not pass on a spill host runs again in
  the same order, keeping off the hosts its checks failed on (one whose own fault ended them, such as a pause
  taken mid-run, gets one more try once free); a pass on another spill host counts, else the result here does.
  A run whose failures the tests gate (below) proved new on that spill host, the base passing them there, fails
  at once instead: the host is ruled out, so a rerun would only fail again while holding a slot.
  `checks.json` (`spilled_to`), `ttp status` and `ttp checks status` name the host.
  `checks.slurm_overflow` (account-level, unset by default: nothing changes) sends a run that no pool host can take
  (each paused, busy with a device job, full or unreachable, or no pool at all) to a Slurm cluster instead of
  waiting here: `ttp config --account checks.slurm_overflow '{"host": "<cluster-login>", "partition":
  "<cpu-partition>", "workdir": "~/ttp-checks"}'` (only the user sets it; a run may only clear it, with `""` or
  `{}`, and each change is logged in `account-settings.jsonl` under the account folder with who made it and the undo).
  `host` is the submit host's ssh alias, `partition` a CPU partition, `workdir` a folder on the cluster's shared
  file system; `max_jobs` (2) caps this machine's jobs there at once, `--time` is the run's median wall time plus
  `margin_s` (600), at most `max_time_s` (7200, also its value before a median exists), and a job pending past
  `max_pending_s` (1800) is cancelled. Each check is one batch job (`--nodes=1 --ntasks=1`, no GRES) on a copy
  of the commit, copied as for a spill host; the submit host only runs sbatch, squeue, sacct (`scontrol show job`
  when the cluster keeps no accounting) and scancel and serves the job's output and exit code. A job's exit-code
  file may show late on the submit host (NFS): it counts as missing only `rc_grace_s` (90) after the job left the
  queue. Before the first job on a (host, partition, workdir) one tiny probe job checks that a marker written from
  the submit host under `workdir` is readable on the node and that the node's answer shows back; a failure (a
  `workdir` on a disk only the submit host sees) marks the overflow unusable, with why, in `ttp status` and
  `ttp checks --slot-status`, and no check job goes there until the settings change or 6 h pass. On the node the
  job makes the checks' pinned Python env under `workdir` once per lock hash and runs the check in it
  (PYTHONNOUSERSITE=1, PIP_REQUIRE_VIRTUALENV=1); an env that cannot be made is an environment fault. A run with a spill wrapper (`spill_wrap_cmd`) never overflows: the
  batch job runs none. Urgent runs take a free job slot first. A cluster ssh does not reach
  keeps the run waiting here; one whose sbatch refuses the job, or a job that ends for a cluster reason twice
  (it is submitted again once: never started, node fault, preempted), is an environment fault and the run runs
  again here, whose result counts. Stopping the run (a stop, a timeout, a newer head) cancels its job; a job
  whose run died is cancelled by the next overflow pick, and one the submit host stopped answering for still
  ends at its time limit. Every job (id, project, task, check, submit, start and end times, state, exit code) is
  logged in `check-slots/slurm/jobs.jsonl` under the account folder; `ttp status`, `ttp checks status` and
  `checks.json` (`spilled_to`, the submit host) show running ones like a spill host's. A project's own
  `delivery.checks_spill_hosts: []` keeps its runs here, overflow included. After 3 jobs in a row die at start
  (Slurm ends them, not completed, within 30 s of their start and with no output; a job that completes or gives
  output resets the count), the overflow pauses itself: it submits no more jobs, records the trip (when, how
  many, the last job ids with their state and exit code) in `check-slots/slurm/breaker.json`, and one daemon
  raises one `slurm-paused` alert. Runs that would overflow stay in the local queue in their place; a run whose
  job died at start runs again here, whose result counts. Only setting `checks.slurm_overflow` again (or
  clearing it) re-arms it and clears the alert; `ttp status` and `ttp checks status` show the pause in one line.
  `ttp checks status` (also `--slot-status`, `--json`) prints each repository's lane holders (pid, task, run,
  age, alive or dead, what it checks and whether its task has finished) and its waiters in queue order, the next
  one marked; it is read only and never queues. Every lane group has a name (its repository or wrapper lock, else the project
  folder). `status` is the only subcommand: an unknown word or flag before `--` (`ttp checks statuz`,
  `ttp checks --foo`, `status --bogus`, `--slot-status` with a command) is refused with exit 2 and queues
  nothing; check commands go after `--`.
  A task never runs the same suite twice at once: a full `ttp checks` of a task that is still waiting joins
  the same task's checks with the same head, commands, purpose and environment that already wait ahead of it or
  run now. It leaves the queue, runs nothing and reports that run's outcome (passed, failed or timed out, exit 1
  on a failure). A waiter is superseded when the same task queues the same checks on a newer head (a later commit
  time), or, in a worker run, any checks of the same purpose in the same repository (the newer run takes
  the waiter's place in the queue; a review precheck neither yields nor takes a place this way): it reports the newer head's result, naming that head and saying its own head was not checked, never a
  pass of its own. A newer head's checks that ended before the waiter queued supersede nothing: an older head
  queued later (after a reset, or a second worktree) runs its own checks. A run that has started is stopped only when the same task starts checks of the same
  purpose in the same repository on a newer head in another worker run (an earlier attempt's detached checks):
  the newer run takes its place in the queue, so one task never holds two checks lanes; a review precheck is
  never stopped this way. Another command, environment, purpose, task or
  repository is never a duplicate. If the run it follows ends without a result, the waiter queues again in its
  old place. `ttp checks status` and `ttp status` list joined and superseded waiters, and the `--result` probe
  says the run waits on another.
  Each task and check waits once in the lane queue: should two waits for the same check of a task still be
  queued (one did not join, e.g. queued by an older runtime), the older run's wait is cancelled as superseded
  and the newer run keeps the earlier place. A waiter whose process is gone is dropped with a one-line note,
  a waiter whose ticket vanished queues again at its old place, and the head of the queue looks for a free
  lane every half second. A lane still held by a check
  whose task has finished (done, failed, cancelled or exhausted) raises one `checks_lane_finished` event
  (pid, task, run, time since the task ended) once `checks.done_holder_flag_s` past the end (default 300,
  0 turns it off); once `checks.done_holder_grace_s` past it (default 1.5x the check's usual run time,
  else its wall-time cap, else 1 h; never under the flag time) the daemon stops that check, only when it
  is the task's own detached `ttp checks` run, so the lane goes to the next waiter; any other holder is
  only reported. The checks the daemon starts for a code task's review as that task finishes are not a
  finished task's lane while a review that reads them is still open; once it ends, a precheck still
  holding the lane is reported but never stopped (it is no worker run; the wall-time cap below ends it).
  Each lane is named for people: `repository <directory name>` (a short id is added only when two repositories
  share a name), `wrapper lock <command>` for a `checks.wrap_cmd` lane, and a waiter whose lane is free but whose
  project is at its cap waits on `project cap (checks.test_project_max)`; the JSON keeps the stable `id` beside
  the `label`. `ttp status` and the web app show one line per lane: who runs, who has a lane but
  is not running yet (and waits on the host-wide checks slot or test-worker slots), and each held check with its
  priority, its rank now and
  what it waits on; the `checks_slot_wait` event uses the same names. Each holder shows its queue age (from
  joining the queue), its run time (from its first command) and how many child processes it has alive, apart,
  so a holder that queued for hours never reads as a frozen `running 0 min`, and one with no live children
  stands out. Two checks of one repository share its
  lanes by design; checks of different repositories never do. A run's check commands stop at a wall-time cap: on by
  default, 3x the longest of the project's last 20 completed runs within 1-6 h (3 h before 3 runs are known);
  `checks.wall_cap_s` sets it, 0 turns it off. A run past it has its process group ended, frees the slot and is
  recorded as a timeout, never a pass; `ttp status` shows the holder's time against its cap and the waiting
  order. A hung command need not wait for the cap: one whose output has not grown for 10 min is stopped the
  same way and reported as "no output for 10 min" (a timeout, never a pass). A check object's `"silent_s"`
  (seconds) sets that limit for a check that is silent longer by design, and 0 turns it off. A full run of
  the tt-project test suite lists its 15 slowest test calls just above its `ttp-suite:` line. Every check runs in a
  pinned Python env the harness owns, the user site off, so `pip install --user` never changes what a check imports;
  `checks.python_lock` (project setting, default the plugin's lock of pytest and pytest-xdist) names its lock, and
  `ttp checks --freeze-lock <file>` writes one that pins the user site's packages too (tt-project-harness
  push-checks reference, "The pinned Python env"). `checks.wrap_cmd` (project setting, default unset) runs each command given after `ttp checks --`
  behind the project's own lock or queue command, the command passed as separate arguments; a missing wrapper
  fails the checks, and `delivery.push_checks` never go through it. A plain string applies to every repository;
  `{"cmd": "<command>", "repos": ["<repo key>", ...]}` scopes it to the repositories it serves (keys as in
  `delivery.push_checks_by_repo`), and checks elsewhere run unwrapped. A wrapped command the wall-time cap stops
  before any test started is a wrapper or queue timeout (exit 75, `infra_fault: "wrapper-queue-timeout"`), never a
  test failure. A wrapper running its own test pool starts
  each shard, local or over ssh, as `ttp checks-shard -- <cmd>`, so the shard counts toward that host's checks slots.
  A check command that waits on an outside CPU pool (a test gate waiting for a pool slot) gives the run's host-wide
  checks slot up while it waits: it runs `ttp checks-yield` before the wait and `ttp checks-yield --resume` before
  its CPU work, which returns once the run holds a slot again, queued in the normal priority order (urgent first);
  checks queued behind it start meanwhile. Only the host-wide slot goes (its lane and test slots stay). Several
  processes of one command may ask at once: each is answered, the slot stays given up while any waiter that
  yielded lives and none asked to resume since, and one slot comes back for all resumes. Once every such waiter
  died, the run takes a slot again for its further work; the next command never starts without one, and a killed
  run holds none. Outside a run that can yield (no slot, cap off, urgent or
  spilled) both return at once; every waiter on the same pool should yield, so none holds a slot the pool waits on.
  `checks.fallback_lane` (project setting,
  default "", off) is where those commands run while the wrapper's lock is paused (`held by pause on`): a
  wrapper command (a CPU pool's `ttp lock`, say) or `unwrapped` (no wrapper, TTP_NO_DEVICE=1 set). The pass
  line and checks.json (`fallback_lane`) name the lane and the pause; off, or with the fallback paused or
  unable to serve, the run waits on the pause (exit 75), never fails. A pytest `-n N` above
  the grant is lowered with a line saying so. A worker's own pytest with more than one worker takes slots of the
  same pool through `ttp clip` or `ttp sandbox` (at most TTP_TEST_SLOTS; fewer, or a wait of up to 4 minutes
  then one worker, while other runs hold them), held by the test process until it ends or dies; its `-n N`
  outside both, or above TTP_TEST_SLOTS, is refused, and a bare `-n auto` starts one worker. Builds
  (make, ninja, `cmake --build`) take slots of a second host pool of the same size, the build pool (`checks.build_pool`,
  `checks.build_run_max`, `checks.build_project_max`; 0 pool turns it off), so a long build never takes a test slot: workers
  get one run's share as MAKEFLAGS=-j<k> and CMAKE_BUILD_PARALLEL_LEVEL=<k>, and `ttp clip` of a build holds k build
  slots and lowers an explicit -j above k (a bare ninja gets -j k). A worker's own build outside `ttp clip` with
  -j above k, an unlimited `make -j` or a ninja with no -j is refused. `ttp status` and the web app's feed show both
  pools' use. Each daemon
  watches the host's fork rate (/proc/stat): above `checks.churn_forks_s` (800/s) for `checks.churn_window_s`
  (120 s), the owning project's daemon raises one `process-churn:<unit>` observation and, muted or not, appends the
  event (rate, threshold, new processes per unit, top 5 parent command lines, secrets redacted) to
  `state/logs/churn.jsonl`, rotated to `churn.jsonl.1` past 1 MB. It never stops a process. A
  `ttp checks` or push inside a check command does not wait for its parent's slot. Checks that
  gate a push to the push branch (the push queue's batches and `ttp push`) hold the push lock, so
  they never queue behind task checks: each push branch has one priority slot of its own, shared
  host-wide across projects. It counts toward the cap: a push holds it while it waits, so the next
  slot that frees is its, ahead of every waiter, and no new run of any kind starts while the cap's
  runs, push runs and urgent runs together fill the cap. Its tests take the test pool's free slots
  ahead of the queue (fewer workers while the pool is busy), never a lane beyond the pool. Set
  `checks.priority_beyond_cap` true (account-level, env `TTP_CHECKS_PRIORITY_BEYOND_CAP`, default
  off) to run push and urgent checks beyond the cap, each with a reserved test lane beyond the pool,
  as before. The batch log and `ttp status` say when a push holds it (`push priority slot: ...`,
  `push checks next for a slot: ...` while it waits for room); a killed push frees it too. The
  status line counts every run: `host checks: N running of cap M (incl. push P, urgent U)`.
  A push waiting behind another holder of its slot waits `checks.push_priority_wait_s` at most
  (account-level, default 1800 s), then says who held it and takes an ordinary host-wide turn instead.
  Waiters take turns by project, within one by task priority. Checks of priority-1 tasks (live-impact
  work: heal self-fixes and interim mitigations are filed at priority 1) go ahead of other projects'
  waiters, taking turns by project among themselves, but at most `checks.priority_turns` turns in a
  row (default 3; 0 turns this off) while other checks wait, so the rest still progress. No run waits
  without a bound: one that has waited `checks.starve_s` seconds (account-level, env
  `TTP_CHECKS_STARVE_S`, default 3600, about one full suite run; it cannot be turned off) goes ahead of
  later arrivals at its own priority or a less urgent one for the next free slot, and those runs of its
  project leave it their next free test slots. More urgent runs keep their precedence under a backlog,
  but once `checks.priority_turns` N of them (default 3 when it is unset; 0 lets the aged run go first)
  started while it waited, the aged run takes the next turn, so it still starts within a bound; priority-0
  and priority-1 runs are never held by a less urgent aged one (unless `checks.priority_turns` is 0). A run
  at least 4 times cheaper (a targeted run beside full suites) is not held by an aged run of its priority
  or a less urgent one; a landing's run still is. A short targeted run (fewer than `checks.small_tests` tests, account-level, env
  `TTP_CHECKS_SMALL_TESTS`, default 500, 0 off) goes first among its project's waiters at its priority
  in the queue and is held by no longer parked run of its priority or a less urgent one (a landing's
  still holds it); the cap and every other rule hold it as any run. An urgent run (priority 0 or 1: a task owed to the user,
  live-impact work) that would wait with every slot busy starts within about a minute, in this order:
  on a free host of the account's test pool at once (no `checks_spill_after_s` wait first; a paused
  host is never picked); else on a reserved urgent slot, the next one free within the cap ahead of
  every waiter, taking test slots ahead of the queue past its project's cap (`checks.urgent_slots`,
  account-level, env `TTP_CHECKS_URGENT_SLOTS`, default 1; 0 turns them off; with
  `checks.priority_beyond_cap` true they run beyond the cap, at most that many at once); else, once it has waited
  `checks.preempt_after_s` from its arrival (account-level, env `TTP_CHECKS_PREEMPT_AFTER_S`, default
  120; 0 asks at once), it asks one routine run on a cap slot to step back, once per request: a run of
  its own project first, then the least urgent, then the latest started. That run ends its current
  command, frees its slots and queues again at its old arrival, ahead of every later arrival of its
  project, keeping every pass and log it recorded (a pinned run keeps only passes on its pinned commit
  and reruns the rest there), and the urgent run takes the freed slot: a short-lived claim keeps it
  from every other waiter while the urgent run lives and still waits for it (one that asked while
  waiting for its test slots keeps it while the preempted run stops, up to 60 s from its ask, and lets
  it go if it still cannot start 3 s after the slot frees), and lapses within 30 s
  otherwise (a lapsed claim's request asks nothing). An urgent run waiting for its test slots asks
  only a run whose stop leaves its project's test budget and the host's test pool room for it; else
  nobody. Its log and its task's notes say `preempted by urgent #<task>`, its queue entry in
  `ttp status` says `requeued after preemption by urgent #<task>` until it holds a slot again, and its
  own rerun goes on to its result. A push check and a run behind a lock (the `checks.wrap_cmd` wrapper, or
  commands that run `ttp lock`, `ttp devq` or `flock`) are never asked, nor is another urgent run, with
  one exception: a run whose task owns an open obligation (owed to the user) asks an urgent run that owes
  nothing, after every routine run, so owed work never waits behind urgent work that owes nothing. An
  owed run is never asked. When a reserved urgent slot frees, the waiting owed run gets it first, then
  the urgent run that has waited longest, never whichever happens to look first. `ttp checks --preempt
  <run>` sends the same request by hand to run `<run>`'s checks, with the asker logged in the host's
  check-slots folder (`preempt-asks.jsonl`); it refuses push checks, owed runs, runs behind a lock and
  runs on a reserved urgent slot. The freed slot goes to the next waiter in queue order. A run whose task's
  priority changes while it waits (raised to urgent or lowered) notices within 30 s and re-executes to
  queue at the new priority, at the same arrival. `ttp status` shows `urgent slot beyond the cap: ...` while one runs. While any run waits for test slots, push
  checks use only their branch's reserved test slot. There is no
  separate outage marker: priority 1 is the signal. Accepted gap: while a host mixes runtimes from
  before this, their waiters may disagree on who is next and a slot can sit idle until another frees.
  `ttp push --own` publishes a task's branch; by default its checks take its remote's own-branch
  push lane (one priority slot and reserved test lane shared by every own-branch push, one at a time),
  so they never wait behind long task checks; `delivery.own_push_reserved_lane` false queues them like
  task checks.
  Waiters take turns by project, and within a project by task priority, then arrival; a task that
  is a required owner of an open obligation goes first in its project (its queue entry in `ttp status`
  says so) until the obligation has its receipt or is dropped. Turns between projects do not change.
  A detached `ttp checks` still waiting for its lane, checks slot or test slots when a newer runtime
  is installed (seen for 10 s) re-executes itself with it (same pid, one log line), keeping a lane it
  holds and queueing again for the rest at its arrival and priority; once its commands start it is never
  re-executed. A cancel (`checks-slot.cancel`, a reaped waiter) also ends a wait for test slots.
  The daemon reaps a waiter nobody will read: one whose task ended (however long ago) or is gone, or
  whose run ended without a task (runs of the last 7 days); the cancel and the daemon log say why.
  It also reaps a live task's earlier run's waiting or parked checks once the task moved to a newer
  run (re-dispatched, or its `retry_when` moved to the newer run's checks), so one task never holds two
  checks runs: they leave the queue and free their repository lane. It keeps them while a live task's
  wait (`retry_when`, `then`, `on_pass_cmd`, `next_step`) names that run, or another run joined or
  follows them. Checks already running their commands (holding a host-wide checks slot) run to their end.
- Shared check failures: a task waiting on its own detached `ttp checks` that failed only on pytest
  ids (the failing command's `FAILED`/`ERROR path::name` lines, complete against pytest's summary)
  that also failed in at least `checks.shared_min_tasks` (default 2) other changes' checks on other
  heads (one change's code, review, review-fix and `continue:` tasks count as one; its own never count) within `checks.shared_window_h` (default 6), or on the push queue's known-bad tip, is held
  asleep instead of woken at its tier. Its checks run again on the same head, model-free, once the
  push branch's tip moves past a commit changing a failing test's file, or after
  `checks.shared_backoff_h` (default 2); after 3 such reruns it wakes as before. Each shared failure
  is one incident with one fix task and one low feed line. While that fix is open, in review or
  waiting in the push queue, a new failure of the same ids joins its incident and adds no task. A
  failure no fix owns first reruns only its failing ids on the push-branch tip, model-free, in the process sandbox like any check, and bounded
  by `checks.shared_tip_timeout_s` (default 900): when they pass there, no fix task is added and the
  held tasks wake to rebase onto the tip; when they fail there too, or the rerun proves nothing, the
  incident opens as above. The daily audit lists the tasks held and
  the wakes avoided with their estimated $. A failing id whose earlier incident's fix task ended done
  and landed through the push queue within `checks.shared_repeat_h` (default 72) makes a repeat: its fix task runs at standard and must give
  the root cause, use a fake clock or event in place of wall-time assertions, and pass 20 runs of
  each repeated test under parallel load at the default worker count before done. Any other runner, a cut list or one failing id not seen
  elsewhere wakes the task as before. A task still lands only after its own checks pass.
- Tests gate judging (`gatejudge`): when a plain local pytest check of `ttp checks` fails with a complete
  list of at most `checks.judge_max_ids` (default 10) failing ids, only those ids run again at once, on
  the same host and in the same checks slot, never through the queue: once on the head, then those
  failing again once on the head's base (each rerun bounded by `checks.judge_rerun_s`, default 600).
  The base is the commit the head was built on: the fork point recorded when the task's worktree was
  made (`refs/ttp/fork-point/t<id>`; a continuing task inherits the one it continues, one started early
  on an unlanded change has none), else the head's merge base with the branch the task is stacked on
  (its `pr_branch` or `rebases:` branch), else its merge base with the push target (always, for a
  review or harness task, or with no task); a candidate that is
  not an ancestor of the head (a fork point rebased away) is skipped, and a later candidate that
  descends from the first (the head merged or was rebased onto a newer tip) wins. The check output and
  its excused record name the base and why. Failing on both the same way (the same first error line or
  summary message, and the same failing statement) is pre-existing and does not block (reported by name);
  failing on the base another way blocks. A rerun's selection stays in its own pytest process: a pytest a
  test starts never inherits it. Passing on the head rerun is flaky: it goes into the project's quarantine
  (`state/test_quarantine.json`: first seen, evidence, expiry after `checks.quarantine_days`, default 7),
  which `ttp status` shows, one fix note goes to the coordinator, and it does not block. Failing on the
  head rerun and passing on (or missing from) the base blocks, as does a rerun that cannot tell. Only the
  tests proven new (a conclusive head rerun failed them and a real base result on the same host passed or
  lacked them) are saved per head tree and check (`state/gate_new.json`, kept 7 days): a later `ttp checks`
  of the same tree fails at once, before it queues for a checks slot, naming the tests to fix (`--fresh` runs
  it again). A saved
  base result counts only when it ran on the same host for the same base commit. A quarantined test the
  change does not touch is excused without a rerun. Nothing in the repository is edited or skipped. An
  excused check counts for this change's landing only: the run reads `LANDING ONLY`, its pass is never
  recorded for reuse, and `ttp checks --release` (a release cut's or a required whole-suite check)
  excuses nothing and joins no other run. `checks.judge: false` turns judging off.
- Quarantine lifts: a quarantined test leaves the quarantine early in two ways only. Every 10 minutes the
  daemon lifts one whose fix landed: a done code task whose title names the test (its function or full
  id; a spec that only mentions it does not count), updated since it was first seen flaky, that
  `ttp landed --task <id>` finds on the push branch and that was not already in the head the test last
  flaked at. It reads local refs only. Or `ttp quarantine lift <test id> --why <text>` lifts one by hand.
  Each lift is logged with the test id, the reason and the commit in `state/test_quarantine_lifts.jsonl`;
  `ttp status` shows the last day's lifts and the daily review's audit lists them. A test that is still
  flaky fails again, passes its rerun and is quarantined anew. No existence check lifts a test: an id
  whose file or function is not found at the push branch tip is marked "not found at the push-branch
  tip; lift by hand if the test was removed" in `ttp quarantine list`, `ttp status` and the audit, since
  a test only on a change branch or an id relative to a pytest rootdir in a subfolder reads the same.
  Accepted gap: a deleted quarantined test stays listed until lifted by hand (or its entry expires).
  `ttp quarantine list` shows the quarantine and the last day's lifts. The file is rewritten whole (a
  temporary file renamed over it), so never edit it by hand.
- Checks receive a writable, absolute `TTP_RUN_DIR`, never empty or `/`. Foreground `ttp checks`
  uses the worker's run directory. Detached checks, pushes and push-queue batches each
  get a unique directory under the project's `state/check-reports/`, outside the checked worktree.
  Write reports there, for example `echo passed > "$TTP_RUN_DIR/x"`. Outputs survive worker cleanup
  and remain after completion. Their `TMPDIR`/`TMP`/`TEMP` scratch lives separately under
  `state/check-runs/`; the whole allocation is removed after its owner and last command exit
  (or by later recovery). Git discovery in scratch stops before reaching the enclosing project,
  while discovery in the checked worktree works normally. Scratch and lifetime locks never
  follow symlinks. `checks.pid` and push queue markers record `check_run_dir` and `check_report_dir`; queued launches
  rebuild missing directories from project state after a daemon restart. A rebooted check invocation
  gets a fresh directory. An invalid or unwritable output directory fails before any check runs.
  Foreground pushes keep progress notes and lock-wait accounting in the worker's original run directory.
  Detached launches do not inherit the worker's `TTP_PIDNS` or `TTP_CGROUP`.
- `delivery.push_exclude_paths` (globs, off by default) keeps files off the push branch, such as
  task notes or `tmp/`: `["tmp/**", "notes/*.md"]`. `ttp push` and the push queue refuse a change
  when any commit it brings adds or modifies a matching file, and name the files; a commit that
  deletes one passes. A pattern matches a path, a directory by its files, or as an fnmatch glob
  whose `*` also crosses `/`. `ttp push --own` is not affected.
- Each check (in the push queue, `ttp push` and `ttp checks`) runs with first-failure semantics (bash `-e -o pipefail`, `sh -e` without bash), so
  `a; b` or `a | b` fails when `a` fails. Still prefer one required command per entry. A coverage
  map keyed by full commit SHA must reject unknown heads and fail when a required input is missing;
  never skip a check because of an earlier pass or failure, and add a negative control that must fail.
- A harness selector must not need its own future HEAD in a publication whitelist. Use
  `ttp check-plan --purpose harness --definition-commit <full-commit-id> --definition-path checks/harness.json`
  as a mandatory check command, run from anywhere in the repository (it resolves the top level). The independently reviewed JSON
  definition is read from that exact commit, never from its working copy. For example:

  ```json
  {"purpose": "harness", "coverage": {
    "paths": {"checks/selector.py": "<64-character SHA-256 of committed file bytes>",
              "tests/test_selector.py": "<64-character SHA-256 of committed file bytes>"},
    "commands": ["python3 -m pytest -q tests/test_selector.py"]
  }}
  ```

  Bind every input the commands rely on, including imported helpers and fixtures. Paths are
  exact, repository-relative regular files; they are read literally, without glob expansion.
  Each must exist at the checked HEAD, match its digest, and match the working file used by the
  command. A different HEAD with those covered inputs runs the same required commands; this
  says nothing about publication approval or files outside the binding. The command checks the
  HEAD and bound inputs again after each successful command. Commands use the usual check shell
  and venv selection, run in order, stop at the first failure, and never skip or reuse a pass.
  Its own errors exit 65 (identity or coverage), 66 (input or definition) or 20 (git) and print a
  `check-plan:` line on stderr; a failing command keeps its own exit code, so a command that itself
  exits 65, 66 or 20 is told apart only by that stderr line, not by the code.
  Prepare the inputs first, then commit the definition and have its purpose, coverage and commands
  independently reviewed; installation pins that definition commit. There is no self-referential
  requirement to include the definition's or a later selector-preparation commit's own SHA.
  When a separate pinned review is more than a harness needs, `--definition-at-head` (purpose
  harness only, in place of `--definition-commit`) reads the definition from HEAD's own commit, never
  its working copy: a change to a covered input and its refreshed digest land in one commit, with no
  second commit to move the pin, and the change's own review covers the definition. A covered input
  whose digest was not refreshed still fails, and the error prints its digest at HEAD.
  A harness definition may bind a path to `"committed"` instead of a digest, for an input that changes
  often and is checked by its own commands (a watcher's or schedule's JSON): whatever HEAD commits there
  is covered, under a pinned or an at-HEAD definition, with no digest to refresh and no pin to move.
  The file must still exist at HEAD as a regular file and match its working copy. Candidate
  definitions keep exact digests.

  A separate `--purpose candidate` definition keeps exact publication identities:
  `{"purpose":"candidate","candidates":{"<full-candidate-commit-id>":{"paths":{...},"commands":[...]}}}`.
  Each candidate uses the same paths/commands binding; unknown candidate HEADs still reject.
  Purpose must match the committed definition and is never inferred from the current HEAD,
  push mode or an environment variable. Keep all existing mandatory candidate gates alongside
  the harness check; installing harness coverage does not waive them or carry earlier approvals
  to a changed definition. Applicability-gap ownership and retries remain independent of this binding.
  Exit codes are **65** for unknown identity, changed coverage or purpose mismatch, **66** for
  missing/invalid required input or definition, and **20** for Git failure even if Git also prints
  a matching identity. A selected command's nonzero exit code is preserved (for example **42**);
  **0** means every selected command passed under the binding.
- Tag a check entry `"purpose": "harness"` (only that `ttp check-plan --purpose harness ...` command) or
  `"purpose": "candidate"`; untagged entries serve both. `ttp checks --purpose harness` runs the harness and
  untagged entries plus the commands after `--`, lists candidate entries as `not run: candidate publication
  gate`, fails (exit 1) unless a harness entry ran or reused a recorded pass (one skipped by `if_exists`
  or `if_changed` does not count), and records a pass that never counts for a PR, an `on_pass` hand-off
  or `ttp push --own`, and opens no check gap. The candidate `ttp checks` (a code task's default) and
  `ttp push --own` skip harness entries and report them skipped, never passed: the plan belongs to the
  harness's own repo, not to the candidate's. `ttp push` and the push queue run every entry. Started
  outside the repo that has its definition commit (or outside any git repo), `ttp check-plan` exits 66
  and says so, naming the project's own repo when that one has the commit.
  With no harness entry configured, `ttp checks --purpose harness` runs a default harness plan, all tagged
  harness: `git diff --check` from the push target (else HEAD's parent) to HEAD, leaving out `*.log`, and
  `python3 -m pytest -q runtime/tests` and `bin/tests`, each only where that folder exists (`if_exists`:
  a missing one is reported skipped, never passed). A configured harness entry replaces the whole plan.
- A project whose harness repo is not its code repo gives each repo its own list with
  `delivery.push_checks_by_repo`: a map from `"harness"` (the project's own harness repo), a repo path
  (absolute, `~`, or relative to the project root) or a main checkout's folder name to a list in the
  `push_checks` form, e.g. `{"harness": ["python3 -m pytest -q runtime/tests"], "my-repo": ["make test"]}`.
  `ttp checks`, `ttp push`, `ttp land` and the push queue run the list of the repo they check;
  `delivery.push_checks` still covers every repo the map does not name. The harness repo never
  inherits the code repo's `push_checks`: with no `"harness"` entry, `ttp checks` there says
  `no repo checks configured for the harness repo` and runs the built-in harness default above
  (`--purpose harness`: only `push_checks`' purpose harness entries, else that default). Its pass is
  the harness repo's own, never a pass of the code checks; `checks.json` records the list's source in
  `checks_from`. `ttp config` refuses a malformed map.
- Which tree `ttp checks` checks: the git worktree that contains `--worktree <path>` when given, else the
  shell's current directory, at its top and on its HEAD. It never picks the task's worktree or the
  project root by itself, so a shell left in the project root checks the project root. Every start
  (also `--detach`) and the `--result` probe print `ttp checks: checking <head> (branch <b>) in worktree
  <path>`. It prints a `WARNING` naming both paths, and does not refuse, in two cases: the checked tree
  is not the one the caller is in (the shell's directory or the run's working directory) and it is the
  project root or stale (its HEAD lacks the push branch's tip as last fetched), or the project root
  itself is checked while stale. Check a candidate built in a separate worktree from that worktree,
  and confirm the printed head is the candidate commit before you trust the result.
- `ttp checks` refuses a worktree with uncommitted changes, except unstaged edits to tracked files the
  repository's ignore rules match: a log a running watcher appends to (add it to `.gitignore`) no longer
  forces a fresh worktree. The run lists them; a staged edit to such a file still blocks.
  `ttp checks --purpose harness` also tolerates unstaged edits to harness state no worker's change
  owns: `*.jsonl` records and files under a `state/` directory, and in the project's own harness repo
  `project.json`, `schedules.json`, `CHARTER*`, `MEMORY.md` and `memory/`. It lists them; any other
  uncommitted edit, or a staged one, still blocks.
- tt-project runs no whitespace check of its own. A `git diff --check <base> HEAD` in
  `push_checks` also fails on committed run logs, whose captured lines keep trailing whitespace.
  Leave them out with a pathspec: `git diff --check <base> HEAD -- . ':(exclude)*.log'`. Files
  that `.gitattributes` marks `-diff` or `binary` are skipped by git already. Checks stay as
  configured; `ttp doctor` names a `git diff --check` that does not leave out `*.log`.
- A bare pytest in a check (`pytest`, `py.test`, `python3 -m pytest`, also after `env` or
  `timeout <n>`) runs as `<venv>/bin/python -m pytest` when the repository has a `.venv/` or
  `venv/` with pytest in it (in the worktree, else in its main checkout, else the active venv: a
  run's is the project's, and `ttp checks` activates the project's venv when none is). The main checkout's venv
  is not used when it has the project installed editable from the main checkout: a worktree's tests
  would import that code and could pass falsely. The check then runs as written and its output says
  why. A check that fails at import because a package is not installed is reported by `ttp checks`
  and `ttp doctor` as an environment problem, not as the head's code (as an environment problem
  also, when tests failed too): point the check at a venv. Before it runs checks, `ttp checks` warns
  of a venv whose `bin/python -c pass` fails (a dangling `.venv` link, or a venv made on another OS)
  and prints the command that makes it again.
- Fast path (opt-in, off by default): with no `delivery.checks_fast_globs`, every diff runs the
  project's full checks. A project that lists globs there (for example `["docs/*.md"]`) gets the
  fast path: when every changed path matches one and none matches `delivery.checks_fast_exclude`
  (by default code, scripts, manifests, tests, templates and skill/prompt files; a list replaces
  it), `ttp checks` runs only the whitespace and CRLF checks and says so. That pass is not a full
  pass: it never counts for a PR or `on_pass`. `ttp checks --fresh` (or `--full`, where it is not
  refused) runs the project's checks anyway (it is kept when the checks are queued or relaunched after a reboot), and a full pass
  already recorded for the tree is reused. A scoped `push_checks` entry that applies also turns it
  off. Known gap: a project that opts in owns its globs. A glob that covers installed or
  test-asserted content (for example a plugin README that tests read) skips the checks those
  changes need; list only paths nothing installs or tests.
- Targeted task checks (on by default): a task's `ttp checks` narrows each project check that is one
  plain local pytest run of listed test files to the tests its diff from the push target can reach.
  A changed runtime module runs the case files and tests that import or name it (a module most tests
  name: those naming the functions it changes); a changed test or case file runs itself; any other
  file runs the tests naming it. The whole check runs instead on a conftest, a shared test helper or
  fixture, the checks runner, a manifest or `__init__.py`, a pytest or package config file, a file
  nothing places, or a selection of half the suite or more. `tests/test_plugin_catalogue.py`
  always runs (`delivery.checks_targeted_always`, a list of test files). The output and the run's
  `checks.json` (`scope`) say `targeted: N case files, ...` or `full: <reason>`.
  A targeted pass counts only for that narrowed run: it never counts as the full check's pass and
  sets no verified-through marker. `ttp push` and the push queue never narrow: every batch runs the
  full required checks. `delivery.checks_targeted: false` (or `ttp checks --full`) restores full
  runs. With the push queue on, the push train's full run on the merged tree (bisected to the changes
  at fault when it fails) is the only full suite, so a task's or review's own checks never run in
  full: a change that would widen to the whole suite (a conftest, a shared test helper or fixture,
  the checks runner's own files, a manifest, `__init__.py` or config file, a test file's module-level
  code) runs only the units importing or naming the changed file, plus those naming a function or
  fixture it changes, and a selection of half the suite or more stays targeted. A task's
  `ttp checks --full` is refused with a one-line reason unless the user set
  `delivery.checks_task_full: true`; `ttp checks --release` (the release cut's required suite) always
  runs in full: never narrowed, refused or fast-pathed. A selection too long for one environment
  variable goes compressed (`TTP_PYTEST_SELECT_FILES_Z`) and stays targeted. `delivery.task_checks`
  (a check list, same form as `push_checks`) is a task's own quick set: a task's or review's
  `ttp checks` runs it, targeted as above, instead of the push checks; unset, it runs the push checks
  (the smoke set that gates a landing). The push train (or `ttp push`) still runs the push checks
  once per batch on the merged heads, drops only the change at fault and lands the rest, and
  `delivery.post_land_checks` runs the slower tests after landing, so nothing lands without its push
  checks passing. A change only the full suite covers adds the tests it can place,
  and the scope line ends `; the push batch runs the rest: <reasons>`. With the push queue off a
  task's checks narrow and widen as before. Each checks run records its wall and CPU seconds
  (`wall_s`, `cpu_s`) in `checks.json`; `ttp stats <name> --landing` shows per day the test runs and
  full suites, CPU-hours and $ per landed change, and approved to landed (median and p90), then over
  the last 24 h and 7 d the booked spend over the commits landed on the push branch (merges and
  version bumps left out) and the subagents those runs started (each run's note keeps
  `subagents_spawned` and `subagent_types`, counted from its Agent calls; their cost is already in the
  run's own). Checks already waiting for a slot re-execute on the new runtime
  and pick their scope again.
- `ttp checks -- <cmd>` adds a check. Several words run as that argv (`ttp checks -- pytest -q`);
  one quoted string runs through the shell (`ttp checks -- 'FOO=1 pytest -q && ruff check .'`);
  several quoted strings run as one check each (`ttp checks -- 'pytest -q' 'ruff check .'`). An
  added check that `delivery.push_checks` already runs on every head, or through a scoped entry
  that applies on this head, is dropped, and said, so it never runs twice. Spellings of one pytest
  run (`pytest`, `python3 -m pytest`, `<venv>/bin/python -m pytest`) count as the same command.
- In Claude Code workers and reviewers, a Bash call that runs one of the configured pytest checks
  in full (every path it names, or a directory above them, and no `-k`, `-m`, `--lf` or node id
  it does not have) is refused with a pointer to `ttp checks`, which reuses a recorded pass.
  Focused runs pass. `TTP_ALLOW_FULL_SUITE=1` before the command (or in the run's environment)
  lets one through; each refusal is a line in the run's `refusals.jsonl`.
- Scope a mandatory check by what it covers, never by whether it would pass. Prefer `if_changed`
  on the paths the check covers, or `if_exists` on the file it runs. Where no file marks the heads a check applies to (say a test that
  is missing at an independently reviewed head of another delivery branch), the fallback is a
  shell conditional on the full SHA in the check itself:
  `h=$(git rev-parse HEAD) || exit 1; [ "$h" = <full 40-character sha> ] || pytest -q tests/test_x.py`.
  A leading assignment such as `h=$(...)` is accepted as a check's first command, like `set -e`.
  Assign first, then compare: `set -e` does not catch a failed command inside a substitution
  in a test, so `[ "$(git rev-parse --verify HEAD)" = "$expected" ]` just compares an empty
  string when git fails. Write `head=$(git rev-parse --verify HEAD) || exit 1` and then test
  `"$head"`.
  Write the condition so that an error in it runs or fails the check, never skips it. Changing
  a check's scope changes what a head was checked against: every head it affects needs a fresh
  review, and earlier approvals do not carry over. Never add blanket "skip if the test file is
  missing" guards (`[ -f tests/test_x.py ] || exit 0`, `|| true`): a missing test, a failed
  assertion and a git error (an unset identity, a bad ref) must still fail the check.
- `ttp push --detach` runs the same push in a process of its own and returns at once, for
  checks that outlast a worker's tool call. It prints a marker under `state/pushes/` and a
  `retry_when` probe, `ttp push --result <marker>`: 1 while the push runs, 0 once it finished,
  printing `ttp push: pushed <sha> to <branch>` (and version) or `ttp push: not pushed (...)` and the log tail. For an `on_pass_cmd`, add `--pushed`: exit 0 only if the push landed, 1 while it runs, 3 if it finished without pushing; no grep needed. A push killed by a
  crash or a reboot frees its locks; the probe (or the daemon) then records it as failed, saying
  whether it died at startup, in a reboot or later, instead of waiting. From a sandbox that runs
  each command in a PID namespace of its own (Codex on Linux), which kills a background process
  when the command ends, the push is queued and the daemon starts it; the probe exits 1 meanwhile.
- `delivery.version_bump` lets `ttp push` own the version bump, so parallel reviews never race for
  one version: `{"files": [...], "changeset_dir": ".changeset"}` (optional `package`, and `paths`,
  default the folder the files share). After each rebase, when the change touches `paths`, it
  sets every listed file one patch version above the tip's and adds a changeset when the change
  brings none, in one `<package>: X.Y.Z (<subject>)` commit (optional `changeset_file`, a repo-relative
  `.md` path: one release changeset per package, which the bump (of `ttp push` and the push
  queue alike) appends the batch subjects to as
  bullets, skipping lines already there, or creates when missing, instead of one file per batch); a later round replaces that commit.
- `delivery.push_queue: true` hands pushing to the daemon. A review that passes approves the
  exact head it reviewed (`"push": [{"branch", "head"}]` in its hand-off) and becomes `pushing`; it
  never runs `ttp push`. Without a model, the daemon replays the approved changes in order onto the
  latest tip of `delivery.push_branch`, adds one version bump and changeset for the batch, runs
  `delivery.push_checks` once and pushes without force. Then the reviews are `done`. Conflicts
  that need no judgment are settled in the batch and judged by those checks: version lines, lines
  both sides added at one spot, and changes of different lines that git calls a conflict only
  because they are adjacent (also a block one side added right beside a line the other only
  reworded or deleted, each line as its side wrote it), edits of different cells of one Markdown
  table row, and numbers in a test file that both sides only raised (two changes each raising one
  prompt size cap: the cap gets both raises, the trailing comment both sides' added reasons). A conflict only in the shared release files (the version files,
  changesets, the release changeset, a CHANGELOG.md) is settled mechanically: both sides' notes
  stay, a changeset both sides created keeps a file each, and the approval holds as long as the
  change's own diff outside those files is still the approved one. A conflict left only in shared
  docs (`delivery.mechanical_merge_paths`, a list of path globs where `*` also crosses `/`; default
  `docs/*.md`, `*/docs/*.md`, `*README.md`, `*CHANGELOG.md` and `*.changeset/*.md`; an empty list
  turns this off) is settled only where both sides just added lines (two changes appending to one
  section): the queue keeps both sides' lines in landing order, the branch's then the change's.
  The approval carries only when the change's own diff outside those docs and the release files is
  still the approved one, no commit of it was left out as landed, the result holds no conflict
  marker and the push checks pass on it; the push log and the review's task events
  (`approval_carried`, `doc_merged`) say so, the task's result records each merged doc
  (`doc_merges`: file, hunks, the change's commit and the landed commit it met), and the Course
  check's self-race line does not count it. A doc hunk where either side edits or removes lines
  needs judgment: it goes back to its review like a conflict in code, tests or manifests. A change
  that really overlaps another goes back to its review to resolve the conflict. An approval that carries an older copy of commits another open change has
  since rewritten goes after that change (in the same batch when both are approved, else it waits
  for that landing, at most two hours), so it is rebased and re-reviewed once instead of losing
  its approval at each of the other's landings; a failed check fails the
  review and wakes the coordinator. When the whole train fails, the batch checks prefixes, and the
  first one that passes every push check pushes at once: it is never checked again and no change
  joins it, and the changes after it go untested and unblamed into the next train on the new tip.
  Only when even the first change fails alone is it blamed; the ones after it are then checked once
  more without it and pushed when they pass. A failed pytest check is judged as `ttp checks` judges one: its failing
  tests (at most 10) not yet judged at that head rerun once there, within `push_rerun_s` (default
  600), and those failing again rerun on the tip alone, on the same host, up to 3 times. A test that
  passes on its rerun is flaky (quarantined), one that fails on the tip too is pre-existing, and a
  quarantined test the batch does not touch is excused at once: when none is new the check passes,
  the batch goes on and the coordinator is told once per test so a fix gets queued. A check that did
  not exit 1, or that pytest stopped early (`-x`, `--maxfail`, an interrupt), is never rerun. A test
  the tip fails on its own is pre-existing for the whole batch, so it blocks none of its changes; tests
  whose id matches `push_hard_tests` (a regex, default `leak`: the leak scan) and a failed check that
  names no test ids still block. The batch's checks spill as `ttp checks` does: with
  `delivery.checks_spill_hosts` set and spill first on, each check that may spill runs on the first free
  spill host with that host's worker grant (a fixed `-n N` above it is lowered to it), the rest here; a
  spill host's own fault (ssh, the host) reruns that check on the next free spill host, else here, and
  never fails or holds the batch. Only a check's first full run spills: every rerun of its failing tests
  (the judge's, the search's) runs here, with its test selection. `ttp checks status` shows each run on a spill host, and each run that
  stayed on the project host with why. The search runs only the new failing tests at each prefix and the full suite once, at the prefix they
  pass on, which then pushes. Keys, all under `delivery`: `push_batch_s` (default 300: with
  no review running or ready an approval starts a batch at once; otherwise the batch starts once the
  oldest approval waited this long; 0 = at once), `push_batch_max`
  (default 8: start at once with this many; it caps nothing: one train takes every approval due), `push_min_gap_s`
  (default 1800: after a batch that did not end clean, such as a failing tip or a dead batch, the
  next starts only once it ended at least this long ago, measured
  from its recorded end so a restart never stretches the wait; none after a clean batch, though a
  new batch never starts while the last one's after_push still runs; a full batch, a
  priority 1-2 review's approval, or one the last batch requeued unchecked after a failing change
  starts at once; 0 = off). Approvals that waited while the last batch ran start the next one as
  it ends, clean or not, and priority 1-2 approvals always lead it; when such fast approvals start a
  train before the window, every other approval due rides in it. `push_age_s` (default 14400, 4 h; 0 = off):
  an approval of priority 3 or worse sorts one priority better per this many seconds it has waited,
  never better than priority 2, so a steady stream of priority 1-2 approvals cannot keep it out of
  batches; aged to priority 2 it is older than that stream and leads it, while priority 1 and urgent
  approvals still go first. `ttp eta` of an approved change counts the approvals ahead of it and the
  measured batch time. Within a priority, an approval an earlier batch
  sent back as a conflict leads, so a peer ahead of it cannot send it back a second time: an older
  routine approval sharing its files is not independent of it, and a solo-path batch takes it first. `push_solo_paths` (default `[]`, off:
  git pathspecs a batch takes at most one routine approved change touching; the oldest such change
  leads the next batch and priority 1-2 ones are never held (by their own priority: one aged by
  `push_age_s` is still held, one per batch). Two prompt changes that fit a size cap
  alone but not together need no such rule: the batch's cap probes find the one that breaks it),
  `push_cap_tests` (a regex of prompt size cap test names, default `(?i)prompt\w*_(?:stays_small|under_its_cap|size)$`; the `$` anchor means a
  parametrized cap test id (`name[param]`) needs a custom `push_cap_tests`:
  when only such tests fail on a change merged in a batch, its review runs again as a light
  trim-to-fit fix told each measured overshoot, at no review attempt, at most twice per change,
  instead of failing; a batch of several changes first runs only these tests of each check that is
  one plain pytest run, and when they fail it finds the change that breaks the cap on them alone,
  then runs the full checks once on the changes before it; these probes are kept per merged tree,
  so a batch rerun after a reboot does not run them again), `push_landing_alert_h` (default 4:
  when approvals have waited this many hours with nothing landed on the push branch and at least
  two batches tried them, one high alert says so, with the attempt count and how many a host
  reboot cut short; it clears when a batch lands; 0 = off), `push_script_torn_escalate_after`
  (default 3: when batches are held this many times in a row because one check failed twice on a
  transient syntax error in a script outside the change, one high `push_script_torn` alert names the
  check, the scripts and the batches; each check counts on its own, and the alert clears once that
  check passes or a batch pushes; 0 = off), `push_setup_fault_escalate_after` (default 3: the same
  for batches held because one check failed before any test ran, after its reruns; once alerted, that
  check's next such fault isolates the entry that brings it instead), `after_push`
  (commands in the same form as `push_checks`, for example a deploy) and `after_push_timeout_s`
  (default 1800). A batch that ends without writing an outcome is retried with a growing pause,
  and three such batches in a row raise a high alert, unless the host rebooted while it ran: then
  it is reported as cut short by that reboot (its time in the home zone), counts no try and no
  pause, raises no alert by itself and runs again as soon as the daemon is back, with only its members still approved, in its order (newer approvals wait for the next batch), so its checks that passed and its test files that passed on the same tree are not run again. A push the remote refuses because the token lacks a scope (GitHub: `.github/workflows/*` without `workflow`) holds only the changes that need it, pushes the rest at once, raises one `push_scope:<scope>` alert and puts the held changes back once `push_scope_check` passes (default a `gh auth status` scope check, every `push_scope_probe_s`, default 600). `after_push` runs after each push in a clean worktree at the pushed commit,
  with `TTP_PUSHED_SHA`, `TTP_PUSHED_VERSION` (the version `version_bump` reads at the pushed
  commit; empty without `version_bump`), `TTP_PUSH_TARGET`, `TTP_PUSH_TASKS`, `TTP_PUSH_BATCH` and
  `TTP_PROJECT` set. A failed `after_push` alerts the coordinator but never fails the reviews:
  their change is already on the branch. A malformed `after_push` (a scalar such as `true`) counts
  as a failed one. After setup and upgrades, use `ttp deployed "$TTP_PUSHED_VERSION"
  --commit "$TTP_PUSHED_SHA" --after-push` as the final verification (after allowing new
  daemons to complete their first tick). This mode returns 0 for done or pending, and 1 for
  failed. A pending merge needs a live daemon, a harness task owner and a known takeover
  time; it keeps one low status line per project (`deploy-pending:<name>`: '<name> will
  switch to <version> by <time>'), which a newer release updates in place and which clears
  with a short line once the project runs the release (quietly when its check fails: the
  deploy-behind alert tells that), and records a model-free check in the deploying project's
  state, due five minutes after takeover. The original deadline survives retries and
  restarts; a new commit of the same version is a new merge with its own, except that a
  harness task already holding an earlier release's merge keeps that earlier deadline. Once
  the deadline passes (also when it is already past after a restart or a reboot), the
  deploying project's daemon runs the host-side takeover itself, `ttp upgrade <name>
  --take-over-from <task>`, which fences that task even while it still shows activity. It
  tries up to three times, five minutes apart, and the check closes only when that project's
  own daemon reports the release (output in `logs/takeover.log`). A try that defers (exit 75)
  while a merge task that is still working holds the merge spends no try: the check waits for
  that task to end, and reports it as a stuck owner after 6 h. A live daemon
  whose files are already at the release but that has not restarted onto them (or not yet
  ticked on them) is pending too, owned by that daemon, and is re-checked after
  `delivery.deploy_restart_wait_s` (default 900). A daemon that
  stops or fails to come up (high), or an upgrade error for this release, alerts at the next
  probe (about a minute), not at takeover. A takeover still behind after its last try, or a
  restart still behind at its re-check, alerts at normal severity. A project behind is one
  ongoing alert (`deploy-behind:<project>`): each later release it misses updates that alert in
  place (the newest release, since when it is behind, why), and only a daemon going down pages
  again. The push queue sends no after_push alert of its own for that failure. The alert clears
  once the project's own daemon reports the release, and a recovered
  daemon returns to pending. Missing ownership/deadlines, current upgrade errors and daemons
  that fail to start remain failures; an older release's upgrade error does not count.
  Keep nonzero upgrade errors as failures in the deploy command; exit 75 means deferred
  (nothing changed) and 76 applied with the restart deferred to the daemon; both need this
  final verification. Ordinary `ttp deployed` remains a strict read-only
  probe for `retry_when`. It also runs, queue on or off, after
  each `ttp push` or `ttp push --detach` that pushed to the push branch (never after `--own`), with
  `TTP_PUSH_RECEIPT` instead of `TTP_PUSH_BATCH`: a receipt under the project's state records it,
  a detached push runs it once its outcome is written and its locks are let go, and the daemon
  turns a failure, a timeout or a killed run into the same alert; the push stands. Off (the default), each review
  pushes with `ttp push --detach` as above. `ttp status` shows one line about the queue (what
  waits, the running batch, the last push and its deploy), the web app a Push queue card, and
  `ttp push --queue` lists the entries, the last 7 days' conflict counts (conflicted, resolved in
  the batch, sent back) and the last 10 batches. Only a rejected push, a failed
  `after_push` and batches that keep dying reach the top section, and only while they last.
- A push queue batch records each check that passes, keyed on the checked head's git tree, the
  exact command and the environment it ran in (the interpreter and venv it uses, the active venv
  and the target tip). A later batch on the same tree, as after a reboot cut the first, skips a
  check recorded there as passed and logs `reused pass from <batch> (tree <short>)`. Only passes
  are recorded: a failed check, and one skipped as not applicable, always run again. A check that
  is one plain pytest run (no second pytest, and no `PYTHONPATH` or `PYTEST_ADDOPTS` of its own)
  also resumes and records its slowest tests. That includes pytest run behind a launcher after a `--`
  word (`launcher.py -- .venv/bin/python -m pytest -q tests`); the launcher keeps its own words. A check
  object's `"pytest": true` marks a check the command does not show as one (a wrapper with no `--`),
  `false` turns the detection off, and leaving it out detects it from the command. Such a check
  also resumes: a bundled pytest plugin records each test file whose every selected test passed,
  and the next run of that check under the same key skips those files and runs the rest; files
  are skipped whole, never single tests, and pytest-xdist runs never resume. The batch never reads
  the passes `ttp checks` records. `ttp push --own` does: a pass `ttp checks` recorded on exactly
  HEAD's tree, of the same commands (or more that start with them) in the same environment (the
  interpreter and venv each uses, told by the binary's content, its version and the installed
  packages, not by where they live, so another worktree of the tree reuses it; the active venv; and
  `PYTHONPATH`, `PYTEST_ADDOPTS`, `PYTEST_PLUGINS` and `PYTEST_DISABLE_PLUGIN_AUTOLOAD`), is reused
  and logged as `reused the pass of <n> check(s) ...`; any difference, or a pass recorded without its
  environment, runs them again, and the push names the part that differed (`not reusing a pass:
  ... interpreter, version differ`). The push queue keys its own passes the same way. Only the task's own branch is pushed that way: the push branch
  always gets its own checks. Records live under the project's state
  (`push_check_passes.json`, newest 200 within 14 days; `push_check_resume/`, newest 20 within 3 days).
- `delivery.backup_remote` (off by default) names a git remote that each finished code task's
  branch (`ttp/t<id>-...`) is pushed to under the same name, fast-forward only: never forced, never
  to `main`, `master`, the push branch or `delivery.base_ref` (a value naming one is refused). A
  push that is not a fast-forward is skipped with one observation. Setting it from chat needs the
  user's word. Separately, a hand-off that finds uncommitted changes to tracked paths in the
  project's main checkout records one observation naming them (once per set of paths); nothing
  there is committed or changed.
- Project lease (`failover.lease_remote`, off by default): one live coordinator per project across
  hosts, so a project restored on a second host while the first is down cannot double-dispatch once
  the first comes back. The lease is the ref `refs/ttp/lease/<identity>` on that git remote (a URL or
  a remote name of the code repository); `<identity>` is `failover.fleet` when set (projects driving
  one fleet share one lease), else the project id. Each write is a compare-and-swap push. Its epoch
  goes up by one per change of holder and reaches runs as `TTP_LEASE_EPOCH`. The daemon takes or
  renews it every `lease_ttl_s`/5 (300 s ttl) before coordinator turns and dispatch; a host that does
  not hold it starts no coordinator turn, worker run, schedule or push, polls and posts nothing to
  chat, and says so in one event, while maintenance and running workers go on. No wall clocks are
  compared: the holder stops at 0.8 ttl after its last renewal, another host takes the lease only
  after seeing it unchanged for 1.25 ttl on its own clock, a restarted daemon on the holder's host
  renews at once, and after a sleep the lease counts as lapsed until renewed. A store that does not
  answer stops dispatch (fail-closed). The remote must be a separate one: the plugin's own
  repository is always refused, and the project's delivery remote unless
  `failover.allow_delivery_remote` is set (only where its rules allow such refs). `ttp lease check`
  says whether the store may be used and answers; `ttp status` shows one `lease:` line. Not to be
  confused with provider failover (`coordinator.provider_failover_*`).
- `delivery.own_push_remote` (unset by default) names where own-branch publishes go: a git remote's
  name, a URL, or a map from a repo (its path, or its main checkout's folder name; `"*"` for any
  other) to one. Unset, it is the remote of `delivery.push_branch`, else `origin`, and
  `ttp push --own` refuses, pushing nothing, when that remote is a local path or `file://` URL that
  is a non-bare working copy or lies inside another project's folder (`ttp list`), naming this
  setting. A local path set here explicitly is used as given; network remotes are unchanged. The
  `existing_only` branch check below asks the configured target.
- `delivery.rebase_replace` (`new_branch` when unset) says where a rebase task (a `rebases:<branch>`
  label) delivers. `new_branch`: on its own new branch, never onto the branch it rebases, and no
  push is ever forced; a non-fast-forward `ttp push --own` onto an existing remote branch is refused,
  naming this setting. `lease`: `--own` replaces the rebased branch under a lease, as below.
  `delivery.never_force` (default false), when true, refuses every forced push in a run: a raw
  `git push` with `--force`, `-f`, `--force-with-lease`, `--force-if-includes` or a `+refspec`, and
  a lease push by `--own`; config validation rejects it together with `lease`. It also refuses
  moving an existing tag, except a release-channel tag moved forward as below.
- `delivery.channel_tags` (empty when unset) lists release-channel tags: tags a release cut moves
  forward. `ttp push --move-tag <tag> <commit>` moves one, also under `never_force`: it pushes
  `<sha>:refs/tags/<tag>` (`<sha>` the commit's full sha) with `--no-follow-tags` and `--force-with-lease=refs/tags/<tag>:<old>`,
  `<old>` the tag's value on the push remote read fresh. The run's git lets that one push through
  only when the tag is listed, the push updates only that tag on the remote of
  `delivery.push_branch`, the lease matches, the commit descends from the tagged one, and the
  commit is on the remote push branch. A delete, a backward or sideways move, an unlisted tag, a
  missing or stale lease or several refs stay refused. Exit 0 moved (or already there), 2 refused,
  1 the push failed; each move is logged to `state/channel_tags.jsonl`. Creating a tag is unchanged.
  The coordinator may set `new_branch` and `never_force` true itself, `lease` only on the user's
  word.
- `delivery.own_push` (`existing_only` when unset) says which remote branches besides the push branch
  `ttp push --own` may push to, and every push that publishes as with `--own` (a notes branch, a
  `pr_branch:` or `rebases:` task). The branch is the one the task was built on (its latest
  `pr_branch:` record, else its `rebases:` label, else that of the task it continues); under
  `delivery.rebase_replace` `lease` a `rebases:` branch is replaced only when every commit it gained since the rebase started (the tip the
  dispatcher built the task on, recorded as `refs/ttp/rebase-start/t<id>`; without one, every commit)
  is in HEAD by patch, else `--own` refuses, naming them, and the task rebases again onto the new
  tip. It then goes under a lease on the tip `--own` read before its checks, never force-pushed
  plainly or created, and a `pr_branch:` branch only fast-forwards. `on`, set explicitly: the task's own or carried branch, created
  on the remote when new. `existing_only`: only onto a branch the remote already has; it never
  creates one. Unset reads as `existing_only` with one exception: a code task's `ttp push --own`
  still creates its own `ttp/t<id>-...` branch while `delivery.draft_prs` is on, the documented
  way a draft PR's branch is first published; a review, another task's branch, a notes branch or
  a push outside a run never creates one. A code task refused creating another task's branch it
  carries is told that route: its own branch, published with `ttp push --own-branch`. So a
  review of a `pr_branch:` or `rebases:` task whose branch the remote lacks is review only, or approves into the push queue when it is on. The
  refusal names the setting. `off`: none; `ttp push --own` refuses and names the push queue, a
  `pr_branch:` or `rebases:` task lands on the push branch like any other change, and no spec the
  daemon writes (reviews, fixes, PR fixes, `review.auto_notes`) mentions `--own`. Set it `off`
  when the project may push to one branch only. An unknown value reads as `off`. The coordinator
  may turn it `off` itself; `on` and `existing_only` need the user's word. The default was `on`
  until reviews created remote branches in a project allowed one push branch only.
- `delivery.drafts_during_freeze` (on by default): opening and updating draft PRs needs no one's
  word, even under a project's code freeze. Off, for teams whose freeze also covers drafts: the
  coordinator asks the user before a draft PR under a code freeze, and the ask gate lets that ask
  through (an ask about a draft PR outside a freeze is still refused). Leaving draft always needs
  the user's yes, and human reviewers are never requested, whatever the setting.
- `delivery.pr_body_check` (off by default) turns a project's own PR body rules into a model-free
  guard: a worker's `gh pr create`, and a `gh pr edit` that sets the body, first run the command it
  names on the body (from `--body`, `--body-file` or `-F`; on stdin and as the file in
  `TTP_PR_BODY_FILE`, with `TTP_PR_REPO` and `TTP_PR_ACTION`). Exit 0 lets gh run; any other exit
  refuses the call and shows the check's output. A command string, or
  `{"run": "scripts/check_pr_body", "timeout_s": 30, "repos": ["my-org/*"], "skip_repos": ["my-org/sandbox-*"]}`
  (globs on `owner/name`; `timeout_s` defaults to 30, at most 300). While a check applies, a body gh
  would get some other way (`--fill`, `--editor`, a template) is refused with a hint to pass
  `--body-file`, and a check that cannot run (no such command, a timeout) refuses with its error.
  Edits that leave the body alone are not checked. It also covers PR bodies written through
  `gh api`: a REST POST or PATCH to `repos/<owner>/<repo>/pulls[/<n>]` with a `body` field or an
  `--input` JSON `body`, and a GraphQL `createPullRequest` or `updatePullRequest` with a body inline
  or in a variable; a GraphQL call's repo is not told, so the globs do not skip it, and a body that
  cannot be read (stdin not given, a missing file or variable) is refused. A PR is an issue, so
  `gh issue edit` with a body, a REST POST or PATCH to `repos/<owner>/<repo>/issues/<n>` with a body
  and a GraphQL `updateIssue` are checked too when the target is a PR: the guard asks gh whether
  `pulls/<n>` (or the node) exists, skips the check on a 404 (a plain issue) and checks when it
  cannot tell (offline, no auth, a timeout). `repositories/<id>/...` paths are covered like
  `repos/<owner>/<repo>/...` and, naming no repo, are always checked. Put the project's own rules (such as labels its
  PR bodies must start with) in the command, never in the plugin.
- Text for people other than the user (posts, comments, emails and the post-ready drafts a project
  hands the user's agent): answer what was asked, first and plainly, like a colleague; skip side
  details the reader can live without (numbers, lists or internal ids only when they bear on the
  question or the reader needs them to act). The worker and coordinator prompts and the relay skill
  say so. It is guidance, not a gate: no guard checks a post's length or wording.
- `delivery.ai_tag` (default `[authored by AI]`; `""` turns it off) is the AI-authorship tag that
  everything a run posts where people other than the user read it ends with. Prompts state the rule,
  and two model-free guards refuse a post whose last text line lacks the tag (or a similar disclosure
  such as "Generated with Claude Code"; trailing HTML and a hidden `<!-- ttp -->` line may follow):
  the worker `gh` wrapper for `gh issue create`, `gh issue comment`, `gh pr comment`, `gh pr review`,
  the `--comment` of `gh issue|pr close|reopen`, the notes of `gh release create|edit`, and comment,
  review, issue, release, gist-comment or team-discussion posts through `gh api` (REST and GraphQL);
  for Claude Code runs, a PreToolUse hook on connector tools (`mcp__*`) whose name says they send,
  post, draft or create a message, comment, issue, page or email (Slack, Jira, Confluence, mail), or
  an event or invite that names guests; and the Bash hook (Claude Code and Codex runs) on `curl` or
  `wget` sending a body to a literal GitHub, Slack (messages, webhooks), Jira (issues, comments) or
  Confluence (pages, comments) URL that posts (not on a local host); a structured body (Confluence
  storage, Jira ADF, Slack `blocks`) is one post judged by its last text. PR bodies, edits of an issue's or PR's own body and
  commit messages are left as they are, and messages to the user (the chat relay, `ttp notify`, the
  web app) are never tagged: the daemon drops a trailing tag (or a similar disclosure line) from every
  message it records for the user's chats and from a stored hand-off's summary, message and question,
  whoever wrote it. Known gaps, left to the prompt rule: a script or another CLI posting
  (python, httpie, a gh extension), a URL or body built at run time (`$URL`, a text ending in `$MSG`),
  GitHub GraphQL over curl, gists, connector tools whose name says neither post nor invite (a shared
  document) and Cursor runs (no harness hook).
- Change windows: the template charter's Policies line `- Change windows: none` means disruptive
  work (restarts, rollouts, anything that disturbs others) may run whenever it is safe, and waiting
  for a lull is never a blocker. A team that works in change windows lists them on that line
  instead (with their time zone): the coordinator then gives such work `start_after` or
  `start_when` for the next window, and an `idle_capacity` event never moves it outside one.
- `delivery.allow_protected_push_branch` (off by default) lets `ttp push` and the push queue
  publish to a push branch that is `main`, `master` or the remote's default branch. Off, such a
  push branch is refused, the config alert and `ttp doctor` say so, and every review is review
  only, never followed by a raw `git push`. On, the push checks, the rebase before the push and
  the no-force rule apply as to any branch. Only the user's word sets it.
  While it is off, refuses the push branch and an open ask names it (a setting hold), every task
  told to land work there waits blocked on that ask, attempts untouched, instead of running into
  the refusal: queued ones before they start, and blocked ones a worker handed off naming the
  setting. A review with a push step still runs, as review only, and hands off blocked to join
  them. The daemon releases them all together once the setting is on or the ask is resolved. When
  the charter says pushes there need no approval while the setting is off and nothing asks about
  it, the daemon itself asks the user once (a blocking restriction ask), once per push branch
  and charter wording, with no coordinator turn.
- `delivery.plugin_version_guard: true` (off by default) is for a repository of plugins whose
  installs upgrade only to a newer version: `ttp push` and the push queue then refuse a change that
  edits `plugins/<name>/` while a manifest there (`.claude-plugin/plugin.json`,
  `.codex-plugin/plugin.json`) keeps the version already on the push branch.
- `delivery.fast_forward_also` (branch names, off by default) keeps more branches in step with
  the push branch, such as a `main` that holds the last good state: after each successful `ttp push`
  or push queue batch, each listed branch on the push remote is moved to the pushed commit, never
  forced and only when its tip there is an ancestor of that commit. The result is read back from
  the remote and recorded in the push outcome, the probe and the landing notice as
  `ff <branch> <sha>` or `not ff <branch>: <reason>` (not an ancestor, missing, rejected). A
  `not ff` is a warning and never undoes the main push. The push branch itself and invalid names
  are refused. Like other delivery keys that widen where work lands, setting it needs the user's
  word, and a charter that forbids pushing to a listed branch wins: leave that branch out.
- `delivery.mirror_remotes` (off by default) sends each pushed commit on to other git remotes,
  such as a mirror of the repository elsewhere: a list of `{remote, branch}` tables (or
  `<remote>/<branch>` strings), e.g. `[{"remote": "mirror", "branch": "dev"}]`, where `remote` is a
  git remote of the project's repository. After each successful `ttp push` or push queue batch,
  each mirror branch is fetched; when its tip is an ancestor of the pushed commit (or the branch
  does not exist yet) that same commit is pushed to it, never forced. A mirror that has diverged is
  left as it is. The result is read back from the mirror and recorded with the
  `delivery.fast_forward_also` lines as `mirror <remote>/<branch> <sha>` or
  `not mirrored <remote>/<branch>: <reason>`; a divergence names both commits. A `not mirrored`
  is a warning in the feed (a push queue event, or a line in the run's notes for a worker's
  `ttp push`) and never fails or undoes the main push. Mirrors run right after the push and
  before `delivery.after_push`, which still runs as before. The push branch itself and invalid
  names are refused, and setting it needs the user's word.
- `delivery.auto_merge_repos` (`owner/repo` names, none by default) lists the repositories whose
  pull requests the project may merge itself. The coordinator sets it only in the turn that carries
  the user's own message saying so, and only adds repos; each change is logged as a `config_grant`
  event naming that message. A charter Policies line granting auto-merge does not turn it on by
  itself; the coordinator sets the key in the turn where the user says so.
- A plan task starts from what is already known: prior work, the organization's docs and chats
  through the connectors you have, available skills, and public work. Skill plugins it recommends
  can be enabled for the project's workers only (`providers.claude.plugin_dirs`).
- Workers and planners never see the coordinator's context. When it gets or starts a task, the
  coordinator thinks it through and writes the plan, or hands the planner every key point, and
  each spec, plan brief and mid-run correction carries a short `Context:` block: the user's intent
  and words with message ids, decisions, the binding Restrictions, earlier findings and failed
  attempts (as result.json paths), relevant memory, and related tasks and files. A plan gives
  each task it proposes the same block.
- On Claude, the part of a worker's prompt that is the same for every task (rules, charter,
  memory) goes in the system prompt, so the next worker, of any kind, reads it from the cache. The
  kind's rules and the task itself go in the user prompt.
- `providers.claude.worker_isolation: true` starts Claude workers and reviewers without your own
  MCP servers, plugins, hooks and user settings; the project's `plugin_dirs` and its hook still
  load. In one measurement it cut a worker's first turn from about 38k to 23k input tokens.
  Isolated workers get no claude.ai account connector unless `mcp_servers` lists it (below):
  `--strict-mcp-config` drops every server not passed in its own config file, and connectors are
  not defined in any file.
  `ttp new` turns it on; projects created earlier keep it off until you set it. Chat workers
  take this value while `providers.chat.worker_isolation` is unset.
- `providers.claude.mcp_servers: ["name", ...]` lists the MCP servers isolated workers still get.
  Each run copies just those entries from your Claude config (local, then `.mcp.json`, then user
  scope) into its own owner-only file in the temp directory, and deletes it when the run ends.
  Listing a server approves it for workers. A name your config does not define is skipped: the
  run still starts, and `ttp doctor` and a low alert name it. Servers that come from a Claude plugin
  are not in those files, so they cannot be listed; add the server with `claude mcp add` to list it.
  To give isolated workers a claude.ai connector, list it by its `claude mcp list` name
  (`"claude.ai <name>"`). Such a run leaves out `--strict-mcp-config` and keeps every other MCP
  server out by name: each server and connector on the last `claude mcp list` (below), connected or
  not, and each one your Claude config defines for the worktree or the project root, except the
  listed ones. Each is denied in the run's settings (`deniedMcpServers`, so it never starts) and its
  tools in `--disallowedTools` (`mcp__<name>`). That needs a list to trust: until the daemon has one
  no older than 12 hours, or while a `plugin_dirs` plugin brings MCP servers of its own, the run
  stays strict without the connector, and `ttp doctor`, the feed and a low alert say why.
- While `worker_isolation` is on, the daemon runs `claude mcp list` from the project root in the
  background (at most every 6 hours, bounded to 90 s) and keeps the answer in the project's state.
  A connected server or claude.ai connector in neither `mcp_servers` nor
  `providers.claude.mcp_withheld` is undecided: the daemon queues one coordinator event naming the
  new ones, and the coordinator lists each in `mcp_servers` (workers get it) or `mcp_withheld`
  (kept out; its pick when unsure). Until then `ttp status` and the web app's feed show one short
  line, `MCP servers not yet decided for workers: ...`; once each is decided nothing shows, and
  `ttp status --verbose` shows the full state: which servers workers get, which are withheld, and
  any listed connector that cannot be kept (see above) with the reason. A newly connected server
  raises a new event. Plugin servers are left out (`mcp_servers` cannot list them). A probe that
  fails or times out shows as unknown in `ttp doctor`, never as a status line; the last good list
  still counts while it is no older than 12 hours.
- With `worker_isolation` off, Claude workers and reviewers still load your own, the project's and
  plugins' MCP servers, but not your claude.ai connectors (about 2k tokens on every call), unless
  the project needs them: `mcp_servers` names one (`"claude.ai <name>"`), or one of its runs called
  a connector's tool (in the 14 days before the first such worker, or any time since). Then all
  connectors load as before.
- `providers.codex.mcp_servers: ["name", ...]` lists the MCP servers Codex workers and reviewers get,
  with or without `worker_isolation`. Each comes from the project's `.mcp.json` (as Claude reads
  it, `${VAR}` expanded in `env` and `headers`) and goes in as one `-c mcp_servers={...}` argument;
  a server whose `command`, `args` or `url` names a variable is skipped, as its value would be on
  the command line. A worker that still
  reads your `config.toml` (isolation off, or a config that sets a model provider) keeps a listed
  server defined there as it is, and every other server there is turned off (`enabled=false`).
  Secrets never go in the arguments or run.json: a stdio server's `env` values reach it through the
  worker's environment (`env_vars`), and an HTTP server's headers through variables
  (`bearer_token_env_var`, `env_http_headers`), all from an owner-only file the run deletes when it
  ends. Codex runs stdio and streamable HTTP servers only. A name it cannot give is skipped and named
  by a low alert; `ttp doctor` runs `codex mcp list --json` with the workers' arguments (offline, no
  sign-in) and prints what is on. With no list, Codex workers get your `config.toml` servers when
  not isolated and none when isolated, as before.
- Claude workers and reviewers get only the built-in tools workers use (`--tools`): Bash, Read,
  Edit, Write, Skill (the plugin skills) and ToolSearch (it loads MCP tools on demand). WebFetch and
  WebSearch come with every task kind but a review; a review gets them only when its title or spec
  links a page outside GitHub, and the `no_internet` restriction takes them from every task. MCP
  servers (your own, or the project's `mcp_servers`) stay usable. Claude Code's bundled skills are
  listed by name only and stay callable; plugin skills stay fully listed. A Claude Code without
  `--tools` gets a denylist instead. Coordinator turns, Codex and Cursor are not changed by this.
- Host loopback is reachable from Claude workers by default. With `providers.claude.write_fence:
  true`, on Linux the Bash sandbox (the write fence) runs each command in a network namespace of its own, so the host's 127.0.0.1 ports (an ssh
  `HostName 127.0.0.1` with a `Port`, a `LocalForward` end, a reverse tunnel's listening port, a
  local web server) would refuse connections. Each fenced command therefore gets forwarders on its
  own 127.0.0.1, through the sandbox's proxy, for the host's loopback listeners at run start and the
  loopback ports in `~/.ssh/config` (up to 32 ports), for the command's whole life, compound
  commands and scripts included. `providers.claude.loopback_ports: [<port>, ...]` adds ports opened
  after the run starts; `false` keeps the sandbox's own namespace with no forwarders. Limits: ports
  below 1024 are not forwarded, the name `localhost` does not resolve inside the sandbox (use
  127.0.0.1), and macOS gets no forwarders. For anything still blocked, a bare `ssh` or `ttp lock`
  command runs outside the sandbox, and so do daemon-run command schedules. Codex workers are
  unchanged: their workspace-write sandbox runs with network access on.
- `sandbox.fence_freed_commands: ["my-ssh", ...]` frees a project's own host-reaching commands (an
  executable in its harness `bin/`, such as a script that wraps ssh) by name, as ssh is freed: the
  Claude sandbox runs `my-ssh ...` (also after `timeout N`) outside the fence as a simple command,
  and the hook refuses it beside `&&`, `;`, a pipe, a file redirect or a substitution. The sandbox
  never frees a line that starts with an assignment (`VAR=x my-ssh ...` stays fenced; measured on
  Claude Code 2.1.296, also with a literal `VAR=x my-ssh *` pattern): set such a variable inside the
  script instead.
  An older entry `{"command": "my-ssh", "env": ["VAR"]}` still loads: the name is freed, the env
  part is ignored with one daemon log line. A name that is
  not an executable file in the harness's `bin/`, or is a shell, a wrapper, `ttp`, `git` or `gh`, is
  left out with one daemon log line saying why. Set it here, never by editing the template's
  FENCE_FREED (hook.py) or FENCE_EXCLUDED (providers/claude.py): those edits make upgrades conflict.
- `providers.<name>.write_dirs: ["notes", ...]` adds directories inside the project root to the
  write fence's writable roots for that provider's workers, while the fence stays on for everything
  else. Each entry is relative to the project root or an absolute path inside it, symlinks resolved.
  An entry that resolves outside the project root, is the project root itself, or covers the
  tt-project folder (charter, memory, config, state) is skipped with an alert naming it and why; a
  directory that does not exist is skipped with a low alert, never created. Unset, the roots are
  unchanged.
- Landing from inside the sandbox needs no setup. The Bash sandbox writes the project's `.git` but not
  the project root's working tree, so a land or review task cannot fast-forward the push branch where
  the project's main checkout has it checked out. `ttp land` then hands that fast-forward to the
  running daemon (state/land-ff/), which does it on the host and reports back within 2 minutes. The
  daemon checks again first: the checkout must belong to the project's repo, have the branch checked
  out at the tip the landing started from, have no uncommitted changes, and the landed commit must
  contain that tip. Otherwise nothing moves, nothing is overwritten and `ttp land` exits 5 (the
  branch moved), 6 (uncommitted changes) or 1 (no daemon took the request, which is then withdrawn).
- `providers.claude.disallowed_tools: ["mcp__<server>__<tool>", "mcp__<server>__*", ...]` lists tools
  Claude workers and reviewers may not call, on top of the tools the harness always denies them; a
  trailing `*` covers every tool whose name starts so, e.g. every tool of one server. Use it to keep
  workers off a server's private-data or write tools while they keep its other tools. A malformed
  entry is refused when set; one written into project.json by hand is skipped and named by
  `ttp doctor` and a low alert.
- `providers.codex.worker_isolation: true` starts Codex workers and reviewers with
  `--ignore-user-config`, so your `config.toml` (its MCP servers, plugins and hooks) stays out;
  sign-in still works. Codex coordinator turns always skip it, and your rules files too, where the
  build has those flags. A config that sets your own model provider, API or ChatGPT endpoint,
  login method or credentials store is kept, since runs need it.
  Codex has no per-run MCP list, so isolated Codex workers get no MCP servers of yours.

### Caps that can hold ready work

Each cap is either safety (it guards a device, a host, spend or a branch) or convenience (it only
paces work). Convenience defaults are sized to the project's own capacity. `ttp upgrade` drops a
convenience key that a project.json still holds at its old fixed default, so the new default applies.
It logs each key it drops and changes nothing else. A value the project set itself is kept.
A ready task held 5 minutes or more by `device.max_tasks` while the device is free and a worker slot
is open raises one `cap_wait` event naming the key.

| Cap | Default | What it holds | Class |
|---|---|---|---|
| `device.max_tasks` | one per device resource, at most the worker slots, at least 2 (old: 2) | device tasks preparing at once | convenience |
| `review.unreviewed_per_pass` | `budget.max_parallel_workers` (old: 2) | reviews the backstop sweep queues per pass | convenience |
| `review.auto_fix_rounds` | 1 (old: 2) | daemon-queued fix and re-review rounds per stack before the coordinator decides | convenience |
| `review.sweep_after_s` | 300 s (old: 1800) | a code task left in review before its review is queued | convenience |
| `review.unreviewed_wait_s` | 1800 s | a code task waiting externally (a busy lock too) before the backstop reviews its head | convenience |
| review sweep cadence (`REVIEW_SWEEP_EVERY_S`) | 300 s | how often both review sweeps look | convenience |
| review settle grace (`UNREVIEWED_GRACE_S`) | 120 s | a task updated this recently before the backstop sweep looks at it | convenience |
| `coordinator.review_stall_s` | 1800 s (old: 14400) | a dependency in review before the coordinator hears of it | convenience |
| `checks.test_project_max` | from cores and memory, at least 2 (`auto`) | a project's suites at once | convenience |
| `delivery.push_batch_s` | 300 s, none after a clean batch | changes gathered into one push | convenience |
| `delivery.push_batch_max` | 8 | approvals that start a push train at once; one train takes every approval due, so it caps nothing | convenience |
| `delivery.push_age_s` | 14400 s (4 h) | a priority 3+ approval in the push queue sorts one priority better per period waited (never past 2) | convenience |
| `coordinator.burst_window_s` | 120 s (0: off) | high events after a turn high events woke, until one follow-up turn reads them all; never user messages, critical or safety-class events | convenience |
| `retry_when` / `start_when` probe (`waiting.poll_s`) | 90 s (20-180 s); only a `start_when` probe of an arbitrary command that keeps saying not-yet backs off, to at most 600 s (`MAX_EVERY_S`) with the wait's length; landing (`landed:#`, ttp landed), job (detach --check), checks --result, push --result, lock --probe, CI, ack-wait, upstream --fixed and date (`test "$(date ...`) probes, and every `retry_when` probe, keep `waiting.poll_s`; a pass wakes the task at once whatever its timer | a waiting or deferred task before its probe runs again | convenience |
| auth re-probe (`AUTH_PROBE_S`) | 900 s | work for a logged-out provider with no login check, before one run checks it | safety (spend) |
| `ttp lock` on a device | one command at a time | device commands | safety |
| suite lock | one full suite per worktree | a second run of the same suite | safety |
| `checks.host_max` | from the host's cores and load | suites on one host | safety |
| `delivery.push_min_gap_s` | 1800 s, only after a batch that did not end clean | the next push | safety |
| `budget.max_parallel_workers` | 6 | worker runs at once | safety (spend) |
| `machine.worker_limit` / `machine.max_workers` | auto (laptops and workstations) / null: binds only under real pressure (then the cores, 2-32), paced down | worker runs at once across all projects on the machine | safety (host) |

### Queue waits over 30 minutes

A task that waits longer than `coordinator.queue_wait_escalate_min` (default 30, 0: off) on a queue,
cap or admission becomes a must-act item for the coordinator, keyed `qwait-<task>-<queue>`. Covered:
the checks queue (a host-wide checks slot, or a worker polling its own detached `ttp checks`), the push
queue, worker slots (the next ready task, never started while every slot is taken), a device or other `ttp lock`
resource, an outside batch pool and CPU admission. The item names the queue, its cap and occupancy, who
holds and waits for each place, and the wait so far. Its unblock options come from config and host state
without a model call: spill to an allowed check host (only when the project or account lists one), raise
a cap within the host's limit (never a cap the user or account set), let it go ahead of routine work,
route to another allowed device, cut the stale queued tail, or cancel it (never a user's own task). A
resource the user paused leaves no option: the user is told once. The push queue's occupancy also counts
pushes that never enter push_queue (`ttp push`, detached or not): the live holder of the branch push lock
is running (with its step and minutes held) and each other running detached push is queued (with its last
`ttp push:` log line). While that holder lives within a push check's time limit (4 h) no wait behind it is
raised; a holder whose pid died frees the lock, so the wait is raised as before.

The coordinator must act on it that turn, in either the per-wake or the persistent session and on every
provider: a `config_set`, a `task_add` or `task_update` naming the key or updating the task, or a reply
naming the key and the safety rule that holds it. With none, the item comes back at the next 30-minute
mark. At most 3 new items are raised a minute, longest waits first. It clears once the wait ends. The daily review's Unblocking quality section shows
`queue_wait_over`: waits over the threshold in 24 h by queue, max and p95 wait, and minutes from the
mark to the first unblock action.

## Standby snapshots

Off by default: with `failover.standby` unset, nothing is written or shipped and `ttp status` shows
no standby line. Set it to keep a copy of the project's state on another machine:

- `failover.standby`: an ssh machine alias (snapshots go to `~/.tt-project/standby/<identity>/`
  there) or an absolute directory, such as a mounted share. `<identity>` is `failover.fleet` when
  set, else the project's id.
- `failover.snapshot_every_s` (900, at least 60): how often the daemon takes one; it also takes
  one soon after a task finishes. A check that finds nothing changed ships nothing.
- `failover.snapshot_keep` (3): how many of the newest snapshots stay.

A snapshot holds a consistent copy of the database (secrets in its key-value table removed and
zeroed), the harness repo as a git bundle, open tasks' branches, the latest run's result, prompt and
run record of each open task, and this project's open machine-ledger changes and pauses. Run
folders, logs, worktrees, `web.token` and credentials stay behind.

Each one arrives in a temporary folder, is checked file by file against its manifest's checksums,
synced, then renamed into place, so a reader never sees a half-written snapshot. The harness bundle
is full at least daily; the others carry only what is new since that full one, so retention keeps
the newest `snapshot_keep` plus the full one they build on.

`ttp status` shows the newest snapshot's age. When the standby has not been known to hold the
current state for `max(1 h, 3 x snapshot_every_s)`, the line says STALE and the daemon raises one
`replica-stale` observation, closed by the next snapshot or check that finds the standby current.

`ttp replicate [--now | --verify | --restore DIR --into PATH [--seq N]]`: without a flag, the
status; `--now` takes and ships one; `--verify` checks every snapshot at the standby (checksums and
chains); `--restore`, run on the standby, makes `PATH/tt-project` from the newest complete snapshot
in `DIR` (or snapshot N). A restore refuses a non-empty target: if one is cut off, remove its folder
and run it again.

## Where things live

| Path | Holds |
|---|---|
| `<project root>/tt-project/` | everything for the project; ignores itself, so nothing gets committed |
| `…/harness/` | the project's own harness (git): charter, memory, config, prompts, runtime, and a `pytest.ini` that isolates its own tests from an enclosing repository's pytest config and `conftest.py` (an upgrade adds it unless the harness has a pytest config of its own) |
| `…/state/` | database, run directories, logs |
| `…/worktrees/` | one git worktree per code task |
| `~/.tt-project/` | per-user registry of projects, secrets (mode 0600), the `ttp` install, account-level settings (`settings.json`), the global spend cache (`global-spend.json`) |

Each project starts from this plugin's template and then improves its own harness from
experience. When a newer tt-project is installed (`ttp setup` from the updated plugin; the skill does
it on first use after an update), each project's daemon merges it into its own harness within an hour
and restarts, keeping running work. A push in flight holds it for `upgrade.push_hold_min` (30)
minutes at most; after that it waits only for a push step (a rebase, the checks, the push itself)
to end and starts between steps. A push whose process is gone holds nothing. While it is held,
`ttp status` and the web app say why and since when. `ttp config <name> upgrade.auto false` turns that off;
`ttp upgrade <name>` then merges by hand. `ttp upgrade` exits 75 when it changed nothing and the
project finishes the upgrade itself (a merge conflict handed to its harness task, or retried by its
daemon under `upgrade.auto`, which retries only a newer version), or when another upgrade or the
daemon's landing of harness worktrees holds it, so a deploy script can count it as deferred. Exit 76
means the harness was upgraded and the restart deferred to the running daemon (it restarts when it
takes the request); `ttp restart` uses 76 the same way. Exit 1 is a real failure, such as a conflict
nothing retries.
Most conflicts settle without a model: changes to different lines, additions at one spot, and a
local edit that upstream now makes too (comments and spacing aside: upstream's version is taken,
the project's stays in the merge's first parent; a line the project removed and upstream reworded
is a real overlap). A project's own rules in a template prompt belong
between `<!-- ttp:local -->` and `<!-- /ttp:local -->` lines (the prompt leaves those lines out):
an upgrade takes upstream's text and puts each block back where it stood. A project's own imports
in a runtime file belong in one block at the end of the file, after the
`# --- project-local imports below ---` line that ends each runtime module whose last statement is an
import, never inside or next to upstream's `from . import ...` lists (leave those as upstream ships
them): an import added to such a list conflicts with every release that changes it, while a block
after that line merges cleanly. A block that already sits right after upstream's last import line
is moved after that line by the upgrade itself, keeping both import lists. When a project only added
lines to a template prompt, the upgrade fences them that way itself. Only a real overlap, such as
the project and upstream rewording the same line, or each adding its own version of one line (the
same leading key, or most of the text alike), goes to a harness task, queued at once with at most one
open; a newer release retargets it. One that ended without landing holds off the next only until a
newer release or an edit to the project's own runtime or template files, never on a timer; one the
project cancelled holds off nothing. When a cancelled task left its merge committed (in a worktree of
the harness or on its own branch), a host-side `ttp upgrade <name>` merges that commit with the
current template and main and applies it with no task, if both merge without a conflict and the
result compiles and imports; otherwise it queues a task as usual.
The same conflict deferred past 48 h, or past two harness tasks that ended without landing it,
reaches the coordinator once as a normal observation, not once per deploy.

## Standing rules and rule cites

Every coordinator digest (the stateless digest, a session's seed and its delta) carries a
"Standing rules" section built without a model call: the charter's Restrictions and Policies,
memory entries marked standing, open user orders and holds the user or another person set, one
line each with a stable id (`R-…`, `P-…`, `M-…`, `o<n>`, `H-<resource>`) and the user message it
came from. Before a hold, deferral, wait, delivery-route pick or action on a held resource, the
coordinator checks it and `ttp said <topic>`, and cites in the action's `why` or `text` the rule
id or user msg #, a safety rule (`safety: <kind>`) or the task whose output it needs
(`needs #<id>`). A hold-type action (a `task_update` to blocked, `start_after`, `start_when`,
`gate`, `depends_on`, the coordinator's own `resource_pause`) that cites none of these is still
applied, then shown once in the next Course check as `uncited hold: …` and counted. The daily
Self-efficiency audit line `rule cites (24 h)` and `ttp audit`'s `rule_cites` give the hold-type
actions, the share that cite (target 100%) and the likely corrections that name a standing rule.
A session coordinator's own CLI holds go through the same check: `ttp task <project> add` or
`set-when` with `--start-after` or a probe (cite it in `--why`) and `ttp pause --resource` or
`--machine` (cite it in `--reason`); an uncited one also prints `uncited hold: …`. A worker's or
the user's commands are not checked.

## Notifications

Alerts go to every attached chat, to the web app, and, if you
install it, to a desktop notifier on your workstation that covers all your projects
(`ttp notifier install`; asked once per user, recorded with `ttp consent notifier yes|no`). Only decisions, reviews, merges, funds and outages notify by default.
`ttp notifier test` checks that desktop notifications show (with sound) and prints the fix if not.

- Web app: the Notifications card on the Overview tab shows the browser's state (on, not allowed yet,
  blocked, unavailable on this address, unsupported), an Allow button and Send test. While the page
  is open in any tab, each new ask, problem and task ready for review pops up once and stays until
  you close it (where the browser supports that). Browsers allow
  notifications only on https or localhost: open the `http://127.0.0.1:…` link `ttp web` prints
  (through its tunnel from another machine), not the box's own hostname.
- Slack (optional): `ttp secret slack --email <your Slack email>` saves a bot token for your user,
  `ttp config <name> notify.slack true` turns it on for a project, and `ttp notifier test --slack`
  sends a test message or prints Slack's exact error. The web app and `ttp doctor` say when Slack is
  not configured or its last send failed.
- Desktop: alerts play a sound and are meant to stay until you close them. Linux: critical urgency with
  no expiry. macOS: `ttp setup`, `ttp upgrade` and `ttp notifier install` build a small `tt-project`
  app in `~/Applications` (no root) that asks for the Persistent style; alerts come from it once macOS
  shows it allowed: its Notifications settings list it with a style, or, where they list no apps
  (current macOS may have no `com.apple.ncprefs.plist`, and `defaults export com.apple.ncprefs -`
  may give an empty dict; what it prints for a missing domain is unverified), usernoted's log shows
  the app's notification delivered or presented with no denial since its first post (click Allow on
  it). Until then, or on a denial, an empty or unreadable log, alerts come from terminal-notifier or
  osascript as before. The app's Info.plist asks for the Alerts (Persistent) style; whether current
  macOS honours that for a new app is not verified, and macOS lets no program set another app's alert
  style. So while the app is not shown allowed, setup and `ttp notifier test` print one line: allow
  tt-project in System Settings > Notifications and set its style to Persistent. `ttp notifier test`
  also says when macOS muted the test for screen sharing (display state) or a Focus mode. All of this
  only reads macOS's settings and log. `"app": false` in
  `~/.tt-project/notifier.json` turns the app off.

Workers can read Slack links you paste, using your Slack connector if you have one.

## Security

- The web app listens on localhost with a per-project token. From another machine,
  `ttp web <name> --tunnel --keep` opens and keeps an SSH local forward to it. The first one is
  asked once per user (`ttp consent tunnel yes|no`, kept in `~/.tt-project/consent.json`); a kept
  tunnel already installed counts as a yes.
- `ttp bridge` lets lab boxes log in to hosts through ssh agents on the user's workstation, so no
  key is ever stored on a box. Configure it on the workstation in `~/.config/tt-project/bridge.json`
  (start from `docs/access-bridge.example.json`; JSON, so the stock Python reads it). Then run
  `ttp bridge setup [BOX...]`, `status`, `doctor`, `stop [--remove]` or `show` (prints, changes nothing).
  - It keeps one ssh session per box and agent as a user service (launchd or systemd, restarted after
    drops and sleep). Each session forwards exactly one agent and links it on the box to
    `~/.ssh/tt-agent/<agent>.sock`.
  - Hosts the box cannot reach by itself are reverse-forwarded to a loopback port on the box (their
    ssh port only), so their source-address limits still see the workstation. Hosts the box reaches by
    itself (GitHub) only get the agent: their traffic never passes through the workstation.
  - The box's `~/.ssh/config` gets one marked block at the top, written after a backup with the
    rest of the file kept. It holds a `Host` entry per host with its `127.0.0.1` port and
    `IdentityAgent ~/.ssh/tt-agent/<agent>.sock`, and nothing else. The tool refuses a block with an
    `IdentityFile`, key text or a proxy; its one proxy line, `ProxyCommand none`, turns off an older
    entry's proxy for the names it forwards.
  - A box's optional `hold_check` (an absolute path on the box, run as `<it> %n %h`) keeps a host
    someone holds refused: it exits 0 for a held host and 1 for a free one, and only that 1 lifts
    the older entry's refusing proxy. A missing or failing check counts as held, so the check can
    keep a hold but never lift one.
  - A project on the box just runs `ssh <host>` or `git` over ssh as usual.
  - It fails closed. While the workstation sleeps or its session is down, the forwards are closed
    and the link dangles, so those logins fail. A run that needs them waits or hands off.
  - It carries ssh only. The GitHub API (`gh`: PRs, CI, comments) is not bridged; read that from
    the workstation side.
  - What is exposed: every process of the box account can use a forwarded agent while its session
    is up, so forward only agents whose keys that account may use. Other accounts on the box can
    open the loopback ports, which reach only the hosts' sshd and still need a key. Nothing listens
    on a non-loopback interface, as long as the box's sshd keeps `GatewayPorts` off (its default):
    `GatewayPorts yes` would open the forwards on every interface. The link folder must be the box account's own, mode 0700 and no
    symlink, or the session exits; the workstation relay listens only on unix sockets (folder 0700,
    sockets 0600) and stops on the same check.
  - The link and the box's ssh config are rewritten only when they change, so a reconnect touches
    nothing on the box.
  - The session's own login key must be allowed to forward an agent: an `authorized_keys` line with
    `restrict` and no `agent-forwarding` gives none (`doctor` says so). Name a key that may forward
    under `login`.
  - A forward entry with `"adopt": true` uses a loopback port another kept forward already serves.
  - A forward entry with `"interface": "<name>"` is for a host the workstation reaches only bound to
    one interface (a VPN used through ssh's `BindInterface`). Its box port goes to a small relay on the
    workstation that connects to the host's port 22 over that interface. The relay ends with the session.
  - Agent `"env"` is the service's own `SSH_AUTH_SOCK`, even when `login` names another agent.
  - **Offline mode (short-lived certificates).** Off unless the config has a `"cert"` section (see the
    example). Each box listed there gets its own key, made on the box by
    `ttp bridge cert renew --make-key` in a tool folder (`key_dir`, mode 0700); its private half never
    leaves the box. While the workstation is online, `renew` signs the key's public half and puts the
    certificate back on the box. The certificate is valid `validity_hours` (default 72) for the listed
    `principals`, only from the box's `source_address`, with no agent, port or X11 forwarding (a pty
    unless `"pty": false`). `ttp bridge cert schedule` runs `renew` every 30 minutes as a user service;
    it signs again once less than half the validity is left.
    - Signers: `"agent-ca"` signs with a CA key held in the workstation's ssh agent or keychain
      (`ssh-keygen -s <ca.pub> -U`; the tool never reads or writes a private key file). `"command"`
      runs an outside signer (a central CA): the box's public key on stdin, the request in
      `TTP_CERT_PRINCIPALS`, `TTP_CERT_VALIDITY_MINUTES`, `TTP_CERT_SOURCE_ADDRESS`, `TTP_CERT_KEY_ID`
      and `TTP_CERT_PERMIT_PTY`, the certificate on stdout. A certificate for another key, from another
      CA, for other principals or addresses, with more rights or a longer life is refused, not installed.
    - Each target user trusts the CA with one line in their own `authorized_keys`, printed by
      `ttp bridge cert trust --print` (`cert-authority,principals="..."` plus no forwarding). Nothing
      edits `authorized_keys`; to undo, delete that line, or `ttp bridge cert remove` on the box side.
    - The box's block sends each `hosts` entry direct with the box's key and certificate only while
      that host's bridge link does not answer and the certificate is valid. Once it has ended, ssh
      prints that it ended and the login fails: every login method is off for that host, so no other
      key is tried. `ttp bridge cert status` shows each box's validity, address and principals (exit 1
      when one is missing or ended).
    - What works offline: ssh logins from the box to hosts it reaches by itself and that trust the CA.
      What does not: hosts the workstation reaches only through its VPN (an `interface` host, such as
      a remote lab behind a site VPN) stay down while it is offline; GitHub over ssh needs the GitHub organisation to add
      the SSH CA; the GitHub API (`gh`) is not bridged at all.
- Workers run with your permissions, in their own worktree; restrictions in the charter are
  passed to every worker and enforced by the provider where it can (for example, no web tools).
- When the user loosens or removes a Restrictions item (a quoted edit, or a `replaces` of a whole
  Restrictions section, judged item by item), adds a scoped exception to one (`excepts`, or a
  `both_hold` text that permits what it bans), or `over` retires one, the daemon queues one
  priority-1 harness task per change naming the project's tools (bin/, hooks/, schedule commands,
  prechecks and heal checks, open tasks' `start_when` probes) that still carry the old item's terms,
  and a `restriction_tools` event says so; the digest shows a "Guards behind the charter" line while
  that task is open. It never edits a tool. A tool is picked only by the item's distinctive terms:
  identifiers, quoted phrases, user-message cites (#N), values with a unit (900 W), machine and
  resource names, or two of its words with one rare in the tool tree. The harness's own words
  (item, Restrictions, scope, limit, exception) never count, and for a removed item neither does a
  word another charter item still states. An `over` naming the done or landed code or harness task,
  or a commit on the project repo's HEAD, that already updated the tools queues nothing.
  The charter guard's refusal says a user's loosening
  goes in as written, and offers the temporary-exception route only when the user's own words make
  it temporary.
- A scoped exception for work you (or, in agent mode, your agent) ordered goes in without new
  words from you when it adds no new risk class: the order names part of its scope and the
  charter's Goals, Resources or Policies name the rest (never a Restrictions item). It still needs
  your own words naming it when it would touch someone's work or data, disturb a machine or service
  others use, speak as you, merge, take a PR out of draft, or involve money or credentials. Your
  own words in a ticket or chat comment you wrote count when your own message (or, in agent mode,
  your agent's) quotes the record and names you as its author, the coordinator cites the record,
  author and quote, and the author matches `coordinator.owner_identities` or the project's git
  identity. The exception records its source and scope and closes only the gate's ask it cites by
  id. Known gaps: an order naming its scope in other words than the exception is refused (the
  coordinator resends it in the order's words); an exception whose whole scope the order names is
  not risk-checked; a message naming both you and someone else beside the record reads as yours.
- A Restrictions edit resting only on your agent's words (`coordinator.agent_orders_as_user`) is read
  by the clauses it changes. The quoted item and the new text are split into clauses (at `,` `;` `:`,
  a sentence's `.`, and before an "and" that starts a held step such as "and post"). A clause both
  sides keep is not read; every held class (posting or messaging as you, a merge, a PR leaving draft
  or a rule keeping pull requests in draft, another person's request, a step affecting other people, a credential or money) is checked on each
  changed clause. So moving a test to other machines in a clause of its own goes in, while rewording
  a clause that holds a merge or post ban stays held for your own words. An added or dropped clause
  (a "Never," cut from the front, an ", except when CI is green," put in the middle) is read with every
  clause of its sentence on both sides, so a ban anywhere in that sentence keeps the edit held. The
  refusal quotes the matched words, the clause and the class. Known gaps: the clause split is a
  heuristic. Where it cannot place a change (a name cut off by a comma), the clauses beside it are read
  too and the edit is refused; your own words still apply it. A clause reworded inside a sentence that
  otherwise stays ("Never merge PRs; no exceptions" to "...; exceptions when CI is green"), or a word in
  one clause that governs another ("Never post: results from box-a or box-b" edited to drop box-b), is
  read only where it stands, so that edit goes in.
- Text from logs, issues and chats is treated as data, not instructions.
- Credential tripwire (credwatch): the runner stats your credential files as each run's agent starts
  and once it ended: gh's hosts file, `~/.git-credentials`, `~/.netrc`, the ssh private keys in `~/.ssh`
  and each provider's login file, plus every file under the write guard's protected paths (its defaults
  and `guards.protected_paths`, in HOME and the account's own home; known_hosts, gh's state file and
  cache folders left out). Only size, mtime, ctime and inode are compared, never contents: no key or
  token is opened or hashed, and ctime still shows a same-size rewrite. A change queues one
  observation for the coordinator naming the run and the path (one per path and minute when several
  overlapping runs saw it). When this machine's change ledger (`ttp machines change list --all`) has
  an entry made while the run ran that names the path, the change was a recorded, deliberate one: the
  observation is normal severity, names the entry, its project and task, asks for no urgent work, and
  the run's hand-off stands. A change of ctime alone (a chmod: same size, mtime and inode) is told at
  normal severity and fails no run. A change made while another run of any project on the host was open
  too (the file's new ctime falls inside both runs' windows, `credwatch.classify`) is told once as
  changed outside these runs and fails no run: at normal severity when rewritten in place at the same
  size, high when the file is new, replaced or resized. Otherwise it is high and quotes the first line
  of the run's transcript that names the path or runs a command that writes such a file (ssh-keygen
  without a scratch `-f`, `gh auth`, `git credential`), or, with none, says the run's commands did not
  show it and gives the file's time, so the search starts there; and the run's hand-off is stored
  failed (a removal, or a change only this run was open for). Provider login
  files count only when removed or
  rewritten to under half their size, since their CLIs rewrite them on every token refresh. Nothing is
  restored automatically: without reading contents the harness cannot tell damage from a real change (a new
  `gh auth login`, a refreshed token, a rotated key, by you or another run at the same time), a restore
  would undo those, and a copy kept beside the file during a run is one more credential an
  unsandboxed agent could read. The observation says what to log in again instead. The test suite
  runs every test, and every process it starts, with HOME, the XDG folders, GH_CONFIG_DIR and git's
  global config (GIT_CONFIG_GLOBAL, GIT_CONFIG_NOSYSTEM) in temp folders. It fails when one of these
  files (protected paths included) changed, or when any file under the real `~/.ssh` or gh config folder was written (stat only:
  size, mtime, ctime and inode, no file there opened or read; known_hosts and sockets left out). ssh
  and ssh-keygen find `~/.ssh` from the account entry, not HOME, so tests pass them paths under their temp folder. Premise
  checks and guard tests run read-only, under a fresh `mktemp -d` HOME. The tripwire is
  `credwatch.snapshot()` and `credwatch.changes()` in `runtime/ttp/credwatch.py`, its path list
  `credwatch.files()`, for other guards to reuse.
- Runs are unsandboxed, so credential stores are kept out of tool calls model-free (credguard.py).
  Claude Code runs get a Read deny rule for gh's hosts file, `~/.git-credentials`, `~/.netrc`, the
  agents' login files (Claude's `.credentials.json`, Codex's `auth.json`), every file in `~/.ssh`
  but public keys, `config`, `known_hosts` and control sockets, and these stores, each guarded only
  where a model read would show a secret and no task needs to read it:
  - AWS: `~/.aws/credentials` (or `AWS_SHARED_CREDENTIALS_FILE`), `sso/` and `cli/` (token caches).
    `config` passes: profiles and regions only.
  - gcloud (`CLOUDSDK_CONFIG`): `credentials.db`, `access_tokens.db`, `legacy_credentials` and
    `application_default_credentials.json`. `configurations/` and `active_config` pass.
  - Azure (`AZURE_CONFIG_DIR`): the token caches and service principal entries. `azureProfile.json`
    and `config` pass.
  - Docker (`DOCKER_CONFIG`): `config.json`, whose `auths` may hold a base64 user and password.
    The rest of `~/.docker` passes.
  - Kubernetes: `~/.kube/config` or each `KUBECONFIG` file (client keys and tokens);
    `kubectl config view` shows the same with secrets hidden. `cache/` passes.
  - PyPI: `~/.pypirc` (upload tokens in plain text). pip's config passes.
  - git: `~/.config/git/credentials`, like `~/.git-credentials`. git's `config` and `ignore` pass.

  The worker hook refuses a Read, Grep or Glob call that reaches one of them (also by a symlink, a
  key made after launch, or a search over a folder holding one: search a narrower folder) and a
  Bash call that names one, also by a glob the shell expands to one (`~/.kube/conf?g`; `ls` and
  `stat` of a glob pass), plus `gh auth token`, `gh auth status --show-token` and
  `git credential fill`. The tools read their own config untouched: kubectl, helm, docker, aws,
  gcloud, az, twine and git, also `--kubeconfig <file>` (kubectl, helm, k9s), twine's
  `--config-file`, and `KUBECONFIG`, `DOCKER_CONFIG`, `AWS_SHARED_CREDENTIALS_FILE`,
  `CLOUDSDK_CONFIG`, `AZURE_CONFIG_DIR`, `GOOGLE_APPLICATION_CREDENTIALS` or `GH_CONFIG_DIR` set to
  one, and `docker --config <dir>`; `ls`, `stat`, `mkdir` or `chmod` of a store's folder (`~/.kube`,
  `~/.docker`, `~/.config/git`, `~/.config/gh`, the cloud folders) passes, any other mention of the
  folder (`grep -r token ~/.kube`, `tar cz ~/.docker`), a glob naming it, `..` or a second mention
  does not.
  ssh, git and gh still read them themselves: `ssh -i <key>`, `-o IdentityFile=`, `ssh-add`,
  `ssh-keygen`, and chmod or test of a key pass, also as the ssh command given to rsync (`-e`,
  `--rsh`, `RSYNC_RSH`) or git (`GIT_SSH_COMMAND`, `-c core.sshCommand=`); a `stat` with a format
  (`-c`, `--format`, `--printf`) passes when nothing pipes or redirects its output, and a path with a
  `..` part never counts as public. A command ssh runs on another host is checked the
  same way (`ssh box 'mkdir -p ~/.ssh'` passes, `ssh box cat ~/.ssh/<key>` does not), quotes inside
  a path do not hide it, and a `find` looking for a private key or credential file by name is
  refused. Known gaps: a path a script builds at run time, a variable read later
  (`cat $KUBECONFIG`), and a recursive read of all of HOME or `~/.config` (`grep -r x ~`,
  `find ~ -name config.json -exec cat {} +`). Provider parity: docs/providers.md (Codex runs get the Bash check only,
  Cursor runs none).
- Pushes go only through `ttp push` and the push queue. Inside a run, the harness's gh refuses `gh api`
  calls that create, move or delete refs or tags, write file contents, merge branches, update a PR's
  branch, write releases or the rules that protect a branch or tag (branch and tag protection,
  rulesets; REST, and GraphQL `createRef`, `updateRef(s)`, `deleteRef`, `createCommitOnBranch`,
  `mergeBranch`, `updatePullRequestBranch` and the branch protection rule and repository ruleset
  mutations), `gh release` create, edit, delete, upload and delete-asset, `gh pr update-branch` and
  `gh repo sync` of a named repo; reads of the same endpoints pass, and the hook refuses the same
  requests sent by curl and the like. PR merges follow the charter's merge policy.
- A run whose task carries the `untrusted_input` label (its input, such as a PR comment, an issue body
  or a CI log, may carry planted instructions) gets three more guards, model-free (untrusted.py). The
  label is read from the task's record; `TTP_UNTRUSTED_INPUT=1` also turns them on (a benchmark cell
  without a project) and nothing turns them off. A run without the label is unchanged.
  - Pushes: git's pre-push hook refuses every ref but the task's own branch (its recorded branch) and
    the push branch, so a new branch, a tag or a delete is refused, and the hook refuses a raw
    `git push` naming the push branch: that branch is reached only through `ttp push`.
  - Writes: `gh api` with a write method, a field or input body or a GraphQL mutation; every other gh
    subcommand but reads (`view`, `list`, `diff`, `checks`, `status` and the like), so `gh pr comment`,
    `gh issue create` and `gh pr review`; curl with a method other than GET, HEAD or OPTIONS, a data
    or form body or an upload; wget with a post body or `--method`. A task that must post carries the
    label `untrusted_write` too, set by whoever writes the task and never read from its wording;
    pushes and credential reads stay refused with it.
  - Credential reads outside the run's worktree, in a Bash command's words (also inside `sh -c`) or a
    Read, Grep or Glob path: a file in a credential folder (`~/.ssh`, `~/.aws`, `~/.azure`, `~/.gnupg`,
    `~/.docker`, `~/.kube`, `~/.password-store`, gh's and gcloud's config, the keyrings), a known
    credential file (`.netrc`, `.git-credentials`, `.npmrc`, `.pypirc`, `.pgpass`, `.my.cnf`, `.env`,
    `auth.json`, `hosts.yml`, `shadow`), a private key or certificate (`.pem`, `.key`, `.p12`, `.pfx`,
    `.jks`, `.ppk`, `.gpg`, `.asc`, `id_*` but `.pub`), a name with token, secret, credential,
    password or API key in it, a process's `environ`, and environment dumps (`printenv`, a bare `env`,
    `export` or `set`).
  The label is set per source, model-free: on a PR-findings fix the daemon queues and on a shared check
  failure's fix (they quote comments, CI and test logs); on a coordinator `task_add` that sends
  `untrusted_input: true` or whose spec carries a peer note's `[note <fp>]` tag; and on the task of a run
  that read web, connector or downloaded text (the hook records the source in the run dir's
  `untrusted-input` file, the guards apply to the rest of that run at once, and the daemon labels the task
  when the run ends). Continuations, review fixes and reopened copies of a labelled task inherit it, and
  nothing clears it. A labelled task never routes to the chat provider; its run note records `untrusted_input`.
  The hook (Claude Code, Codex, Cursor and the stream guard) and the run's `bin/git` and `bin/gh`, which
  scripts reach too, apply them on every provider. Known gaps: a path or command a script or inline code
  builds at run time (git and gh stay guarded), other HTTP clients, a GET carrying data in its URL and
  connector (MCP) writes, which keep the AI-tag guard. `ttp.untrusted.guards_live()` tells routing code
  that these guards ship.
- A session coordinator's Read, Grep and Glob calls are refused on the credential files listed at
  its launch and on what the worker hook's file tool check refuses.
- Guards in the harness are a second line. Give the account the harness uses a fine-grained token
  that can write only the repositories, and where the host allows it only the branches, its
  projects push to, and protect each repository's default branch (required reviews, no force pushes
  or deletions), so a run that gets around a guard still cannot change it.
- Secrets are entered in a terminal (`ttp secret …`), never in chat, and never copied into a project.
  An OpenAI-compatible endpoint's API key: `ttp secret endpoint <alias> --url <base url>` checks it with
  `GET <base url>/models` and saves it with its URL as `endpoint/<alias>`; `ttp secret show` lists it
  without the key.
- A secret of your own: `ttp secret set <name>` stores the value read from stdin (never from the
  command line); `ttp secret generate <name> --keys a,b,c` stores a fresh random token per key as a
  JSON object (`--force` replaces one). Neither prints a value; both are masked like the others.
  `ttp secret env --secret <name>[:<key>]=<VAR> -- <command>` (repeatable) runs a command with one in
  `VAR`, its output masked and its exit code passed on; the command line carries only names.
  A built-in secret goes only by a field (`--secret jev:key=<VAR>`); `jev=<VAR>` is refused. From a
  sandboxed worker, which cannot read the secrets store, the daemon runs the command outside the
  sandbox and the worker gets its masked output and exit code; `state/secret-uses.jsonl` records
  only the secret names, the task and the time. A request the daemon has not started within 90 s, or
  when the worker's call is stopped (SIGINT or SIGTERM), is withdrawn and never runs later; one
  already started keeps running.
- Where keys live is chosen per machine with `ttp secret store <backend>` (no argument: show it). It
  writes `~/.tt-project/secret-store.json`, which holds no secret:
  - `file` (the default): `secrets.json` in the ttp home, mode 0600.
  - `keychain`: the macOS login keychain, service `tt-project-secrets`; written through `security -i`
    on stdin, so no value is ever on a command line.
  - `command -- <fetch command>`: a program run whenever ttp needs the keys; it prints the whole store
    as one JSON object on stdout and exits 0. Read-only: ttp never writes to it. Its stderr is never
    repeated, and a failure names only the program and its exit code.
  - `env`: `TTP_SECRETS_JSON`, read once and removed from the process environment so no child
    inherits it. Read-only. It serves only the one process given the variable (a script, a CI job):
    a process not given it reads the store as unreadable, never as empty. The daemon refuses to start
    on a machine whose store is `env` (exit 2, and drops the variable either way): its runs and the
    hooks the agent CLI starts (outbound scans, the pre-push scan) are not its children and could
    never see it. Where a daemon runs, use `keychain` or `command`, which every process reads itself.
  Keys read from a keychain, command or env store live only in the process's memory and are read
  again at most every 60 s.
- Masking fails closed. When the store cannot be read and no value was ever read in that process, a
  run's last masking step prints `runner: could not mask stored secrets in the run: <cause>` in its
  log and records `unmasked` (the cause, never a value) in `exit.json`; if the daemon cannot read it
  either, it logs that and raises one `secrets_unmasked` event per cause. Outbound posts (`ttp notify`,
  `ttp note --to`, `gh` from a run) and the pre-push scan are refused while the store cannot be read,
  except in a run whose sandbox hides the store by design (the write fence), which is scanned as before.
- A shared or build host keeps no plaintext keys: `ttp secret store <keychain|command|env> --shared`
  marks it. There the file store refuses to save, `ttp new --host --copy-secrets` and `ttp secret push`
  to it are refused on the far side (exit 3), and a `secrets.json` already there is reported by
  `ttp secret store`, `ttp secret show` and `ttp status` (never deleted: move its keys to the store,
  then delete it yourself). A `secrets.json` on a host marked shared whose store is still `file` (the
  marker written by hand) is still read, never written, and reported as such.
  `ttp new --host` copies no keys unless `--copy-secrets` asks for it.
- A central secrets service (a company's vault or broker) plugs in as a `command` store: any program
  that authenticates the way the service wants (a short-lived machine or user identity, not a key
  file) and prints the JSON object. A service needing more (writes, per-key reads) is one more entry in
  `BACKENDS` in `runtime/ttp/secretstore.py`: a load function returning the dict, a save function or
  the read-only one. The worker hook refuses Bash calls naming the keychain item, the configured fetch
  command or `TTP_SECRETS_JSON`.
- Pushes go out from a trusted machine (the owner's workstation) over HTTPS with the forge's own
  credential helper (for GitHub: `git -c credential.helper='!gh auth git-credential' push`), so no
  build box needs a push key; a build box's project hands its commits to that machine to push.
- Workers run without a sandbox by default (no bwrap, no network or PID namespace; sudo works).
  When the agent ends, the runner ends what it left in its process group (but a pid the hand-off lists
  in `leftover_procs` with how and when it stops) and the run's own sandbox trees nothing owns any more:
  a `bwrap` (a write fence's, or one from a run before the unsandboxed default) runs in its own session
  with its proxy listeners (1080, 3128, loopback ports) and outlives the agent otherwise. The daemon's
  sweep, at start and then each pass, ends the project's orphaned sandbox trees of ended runs and writes
  a `sandbox_trees_stopped` receipt; a `ttp detach` job's tree, a live run's and any with a protected,
  device or foreign member stay. The sweep keeps the daemon's step budget (the next tick goes on after
  the last tree it ended), and a tree whose `/proc` reads hang is left for a later pass and logged once;
  the runner's part waits at most a minute. `ttp leftover-procs --sandboxes [--dry-run]` does the same
  on request (exit 2 when it kept a tree or stopped after its 30 s). A project runs unsandboxed once its run lines say `sandbox=none` and
  that command finds no tree.
  `providers.<name>.write_fence: true` turns the provider's write fence on (Claude's Bash sandbox,
  Codex's workspace-write), and `sandbox.checks: true` the process sandbox for checks. Both are
  user-only: only the user's own word turns one on (`ttp config` in their own shell, or the web
  settings page); `ttp config` inside a run (a worker's or a coordinator's) and the coordinator's
  `config_set` refuse the on value on every provider, and allow false or unset.
  `restrictions.no_internet`, which also turns Codex's and Cursor's sandbox on, is a Restriction:
  a run's `ttp config` refuses it in both directions and the coordinator cannot set it. A sandboxed
  worker cannot gain privileges (sudo fails with 'no new privileges'). A privileged
  command the project allows runs on the host through the daemon: `ttp host-run -- <command...>`
  prints its output and exits with its code (126 refused, 124 timed out, 75 no answer from the
  daemon). Only commands in config `runner.host_commands` run, set by the user or the coordinator
  (`ttp config`, or chat's `config_set`), never a worker; both store only a JSON list of entries
  `ttp host-run` accepts, refusing a shell string or a bad entry by name. An entry is a command string (the words a request must equal or start with),
  limited by `runner.host_command_timeout_s` (default 120 s, at most 600 s), or
  `{"cmd": "<command>", "timeout_s": N}`, which gives that command its own limit of up to 3600 s,
  for example `{"cmd": "sudo -n /usr/local/sbin/power-cycle", "timeout_s": 1200}`. A dict entry with
  `"exact": true` matches only a request of exactly its words, so nothing can be appended to it:
  use it for a privileged command, e.g. `{"cmd": "sudo -n systemctl restart <unit>", "exact": true}`
  (`ttp doctor` warns on a plain entry that starts with sudo). A request wrapped exactly as
  `ttp host-run -- ttp lock <resource> -- <command...>` (one resource, no options) matches its command
  under the same rules, so one entry covers both forms, and the daemon holds the lock while it runs. An entry whose `exact` is not true
  or false, or whose `timeout_s` is over 3600, below 1, or not a number, or that is otherwise malformed, is
  refused by `ttp config` and, when read, named in each refusal; it is never clamped. The longest
  matching entry sets the limit; `--timeout S` asks for less, and a longer ask is cut to the
  entry's limit with a note. A command that may run past about 9 minutes (a worker's tool call
  stops at 10) runs as `ttp host-run --detach -- <command...>`: it returns once the daemon started
  it and prints its id. A supervisor in a session of its own runs it under the same limit, so
  neither the worker's end nor a daemon restart stops it, and writes `state/host-run/<id>.out.json`
  (output and exit code), then `<id>.rc`; both are kept for a day. `ttp host-run --status <id>` exits
  0 once it ended, 1 while it is queued or running, and 4 when it is unknown or lost (no answer
  past its limit, e.g. after a reboot), so it serves as a `retry_when`; `ttp host-run --result <id>`
  prints its output and exits with its code. A detached run still running counts as a detached job
  for `ttp drain` (listed, waited for with `--jobs`, recorded as interrupted with `--reboot`), so a
  planned host cycle does not cut it midway; a claim whose supervisor is gone, from an earlier boot
  or past its limit does not. Every run goes to `state/host-run.jsonl`.
- No run writes under the account's credential folders. Before any tool call, the hook
  (pathguard.py) refuses a create, change, move, chmod or delete under `~/.ssh`, gh's config dir
  (`~/.config/gh` or `$GH_CONFIG_DIR`), `~/.git-credentials`, `~/.netrc`, `~/.aws`,
  `~/.docker/config.json`, `~/.kube`, and every path in config `guards.protected_paths`. It reads
  Edit, Write, MultiEdit and NotebookEdit, apply_patch text, MCP tools whose name is not a read
  verb, and shell: redirects, tee, cp, mv, install, rsync, ln, sed and perl `-i`, chmod, chown, rm,
  truncate, touch, mkdir, `dd of=`, curl or wget output files, `sh -c` and `eval` text, interpreter
  one-liners and heredocs (python, perl, node, ruby: a write call or write mode, a redirect, and
  the commands they run through os.system, subprocess, exec or backticks), the script file a
  command runs, and the login commands that write these folders (`gh auth login`, `docker login`,
  `aws configure`, `kubectl config`, `git credential approve`, `ssh-keygen` without `-f`
  elsewhere). It expands `~`, `$HOME`, `${HOME}`, `$XDG_CONFIG_HOME` and `$GH_CONFIG_DIR`, follows
  symlinks, and checks both the run's HOME and the account's own home. Reads pass, also in code
  that runs a command or compares with `>`; code with a write call (a copy too), or that runs a
  write command (`tee`, `cp`, `dd`, ...), a redirect or a login tool whose target it builds, and
  names one of these paths anywhere is refused, since a variable may carry it. A key or config that
  ssh or scp only reads (`-i`, `-F`, also in `GIT_SSH_COMMAND`), handed straight to a run call that
  is a statement of its own, does not count there. Add a path for one project with
  `ttp config guards.protected_paths '["~/.config/example-access"]'`, or for every project in the
  account settings (`~/.tt-project/settings.json`, same key). A setting only adds: the built-in
  folders always stay. Tests and experiments use a scratch `HOME`, `XDG_CONFIG_HOME` and
  `GH_CONFIG_DIR` instead. Not caught: a path the code builds at run time from parts no word of
  the command names. Which tools each provider covers is in providers.md.
- No `ttp` command prints a saved key. `ttp secret check [alias]` tests one (reachable, auth ok or
  auth failed), lists the model ids it serves and marks each `providers.<p>.tiers.*.model` of the
  current project it does not serve, with no key, URL or host in its output, and `ttp secret env <alias> -- <command>` gives it to a command in `OPENAI_API_KEY`
  (URL in `OPENAI_BASE_URL`). A worker's Bash calls that read the secrets store are refused.
- The harness masks every saved secret as `[secret:<name>]` in what it keeps or sends on: the
  database (events, notes, notify text, hand-offs), the daemon log, each run's folder and its agent's
  session transcript once the run ends, and text for Jev and Slack. The secrets store is mode 0600
  and run folders 0700.
- Private keys get the same treatment on every provider, saved or not: any PEM-style
  `BEGIN ... PRIVATE KEY` block (OPENSSH, RSA, EC, DSA, ENCRYPTED, plain) or PGP private key block,
  raw or JSON-escaped, becomes `[private key redacted]`, as does a block cut short or a lone BEGIN or
  END line. Beyond the places above it is masked in `ttp note` text, the `ttp say` outbox, `ttp
  status` and `ttp logs` output and every web app answer. A running worker's own files hold what it
  printed until the run ends; the web app and `ttp status` mask it when they show it. A detached job's
  log is masked once the job's last process exits, and detached checks mask checks.log and checks.out
  as they end. The daemon reads at most 8 MiB of each run file and spends at most
  10 s per run on this; a bigger file is masked by a separate background job, with no lines lost. A block cut short in plain text is masked through its last key
  line, so prose after a quoted marker line is kept. The Claude Code
  and Codex session transcripts are masked at run end; Cursor and the `chat` provider keep none the
  harness knows of.
- A run's outward writes are scanned before they leave, model-free (`ttp/outscan.py`), so a stored
  secret or a token stays home even when text the run read asks to send it: every `gh` call through
  the harness's `bin/gh`, with a gh alias expanded first (its arguments, a shell alias's text, the
  body, notes, `--input`, `-F` and `-F key=@file` and gist files, and stdin wherever gh reads it,
  read once and passed on: `-` or a stdin path, and gist create, variable and secret set, workflow
  run `--json`, ssh-key and gpg-key add and alias import only when their flags leave stdin as the
  input, so a `while read` loop's stdin stays unread and an open idle pipe cannot hang the call; an
  extension or shell alias, whose stdin use is unknown, is refused unless its stdin shows data or
  its end within 3 s (give it a file or `</dev/null`); not `auth login`, whose stdin is gh's own
  token, or `codespace ssh`, which streams it as a session),
  `ttp note --to` / `--reply`, `ttp notify`, and every `git push` through the harness's `bin/git`
  (its pre-push hook, which `--no-verify` does not skip, runs before the repository's own): the
  messages, added lines and file paths of each commit the push sends, merges' own changes and
  annotated tags' messages, but nothing the remote already has (read from the remote with
  `git ls-remote`, not from local tracking refs). A later commit that removes a leak does not clear
  it, since the earlier commit still goes: rewrite that commit. A push whose remote or commits cannot
  be read is refused too. Each is checked for every stored secret
  value (as is and JSON-escaped), common credential shapes (forge, chat and cloud tokens, private-key
  headers) and the project's forbidden-data patterns. A hit is refused with one line naming its class
  (the secret's slot, the shape or the pattern's label), never the text, and nothing is sent. A chat
  post the daemon must still deliver gets the shapes masked as `[redacted:<shape>]`. It is a check on
  these routes, not a sandbox: a tool that talks to the network directly is not scanned.

## Limits

- Not every feature works the same on every provider yet. The feature parity table in
  [providers.md](providers.md#feature-parity) says, per feature, where Codex, Cursor and the `chat`
  provider match Claude Code, where they differ or miss it, and which open task closes each gap.
  Where the harness hook does not run (Cursor; Codex with hooks off or another hook source), its
  guards, the Stop hand-off check and update hand-over by hook are absent: updates arrive through
  `ttp note` and `ttp steer`: a run that ends without a valid hand-off resumes its session once
  where the session can be resumed (Codex; `Daemon._handoff_resume`), and otherwise fails the
  attempt (Cursor, whose chats the harness cannot resume; the next run starts cold).
- Codex and Cursor report tokens but no cost; their spend is estimated from a price table. Set
  your own rates in project.json as `"pricing": {"codex": {"<model>": [input, cached input,
  output]}}` in $ per million tokens (`"default"` covers other models; same for `cursor`).
- A Claude run cut off before its final report is estimated from its streamed tokens. Codex's
  mid-run budget check prices the running token total from its session rollout (`token_count`
  events), so it stops a run mid-turn once it passes its budget, and a cut-off Codex run is
  booked at that token price (a resumed session counts only what this run added). Cursor
  builds that offer `--output-format stream-json` stream their progress: the stall guard sees it,
  and until usage arrives the budget check uses a floor from the text written so far. Older
  Cursor builds print nothing until the end, so set `stall_s` above their longest run. A cut-off
  run with no usage to price (Cursor, or Codex without a readable rollout) is booked at the
  elapsed share of its budget (flagged estimated).
  A run that wrote no output and reported no tokens is booked at $0 (except on an older Cursor
  build, which prints nothing until it ends).
- Codex workers run in `danger-full-access` (no sandbox) by default. With `providers.codex.write_fence:
  true` or the `no_internet` restriction they run in Codex's workspace-write sandbox, with the
  project's state folder, the repository's git folder and the shared lock folder
  (`~/.tt-project/locks`, for `ttp lock` on resources shared across projects) added as writable
  roots. Codex coordinator turns run with `danger-full-access` too, and in its read-only sandbox
  only under the same opt-in. The daemon snapshots the harness folder and the repository checkout
  before each coordinator turn and logs `run N: warning: coordinator turn changed files in ...`
  when the turn changed any (a log line only: no alert, no model call).
- Codex and Cursor coordinator turns run from an empty scratch directory, so the project's
  AGENTS.md and rules stay out of them. Codex's shell and web search tools are off, and Cursor
  runs in ask mode, when the installed CLI offers those switches.
- Codex workers get the harness hook (coordinator updates handed over once, PR draft guard, kill
  guard, full-suite and `ttp say` refusals) as per-run config, never by editing your Codex home. Codex
  runs such a hook only when trusted, so the run trusts it for that call alone, and only when the
  hook is the run's sole hook source: if your Codex home, the repository's `.codex/` folder or an
  enabled plugin may add hooks, the worker runs without it and gets updates from `ttp note` and `ttp steer` (an
  isolated worker skips your config, so only the home `hooks.json` and the repository count then).
  A build without hooks or the trust flag keeps that older behaviour. Tested live on codex-cli 0.160.
- Not on Codex: worker plugins (`plugin_dirs`). Codex loads plugins only from those installed in
  its home (`codex plugin add` copies them there), so a run cannot add one for itself without
  changing your Codex setup; plugins you install yourself load as usual. An isolated worker, which
  skips your `config.toml`, still gets tt-project's own installed skills (enabled by `-c`, only while
  that copy brings no hooks, MCP servers or apps; of several, the one at the runtime's version).
  `ttp setup` (so also a deploy that runs it) and `ttp doctor` keep that copy at the installed
  release: each release folder is also a one-plugin Codex marketplace, and when Codex's cached
  tt-project differs from the release, they run `codex plugin add tt-project@tenstorrent-skills`
  once (with a timeout), first moving the marketplace to the release when its folder serves another
  version, unless it also serves another installed plugin. A git marketplace is refreshed with
  `codex plugin marketplace upgrade` and never moved: the add runs only when it then serves the
  release. A daemon's start and `ttp upgrade` run the same check once per release (bounded in time),
  and a remote machine gets it from the `ttp setup` its runtime shipping runs. Nothing runs without
  `codex` on PATH or a registered tenstorrent-skills marketplace, none at the release, and a failure is
  a printed line, never fatal. A `plugin_dirs` set for
  Codex (or for a Cursor build without `--plugin-dir`) raises one low alert per value and a
  `ttp doctor` line saying workers run without them.
- Coordinator updates reach a running worker on every provider: through the hook after a tool call
  (Claude Code, Codex where the hook runs), else printed by the worker's next `ttp note` or `ttp steer`.
  Where the hook runs (known per run), `ttp note` and `ttp steer` leave updates to it, so a subagent never
  takes one; a `ttp detach` job's `ttp note` never takes one either.
  One still unread after `steer.ack_after_s` (300 s; 0: off) raises one `steer_unread` event to the
  coordinator, naming the task and the update; it is marked handled once the worker reads it.
- Drift flags (model-free, from the live run record): a command failing again and again since it last
  worked, the same tool call in a loop, files a code worker changed outside the paths the spec names
  (never a reviewer; not files its branch changed before the run), a quiet run (no call, note or commit
  for 15 min while not waiting on a lock, checks or a detached job) and spend running ahead of the wall
  clock. Each flag is first told to the worker in its steer.md; still flagged 10 min later with no
  `ttp note` since, it rides along to the coordinator's next turn as `worker_drift`; only a run past 90% of its budget or a
  command failed 6 times wakes the coordinator. Thresholds: `coordinator.drift.*` (0: off).
- Not yet on Cursor: worker plugins, worker isolation, and isolation of coordinator turns from your own CLI config and MCP servers. Cursor
  enforces the `no_internet` restriction only through its sandbox (when the build lists
  `--sandbox` and `--add-dir` and the host can run it; `ttp doctor` says when not) and has no
  plan-window meter; without ask mode it has no
  read-only mode either.
- Cursor has no reasoning-effort flag; tiers map to model names.
- Context compaction per tier (`budget.compact_window_tokens`) works on Claude Code and Codex. A run
  whose fixed prompt (system and task prompt, tool definitions) leaves too little room under its
  tier's window gets a larger one, so it does not compact on every tool result; the run's record
  and the daemon log say so.
- On Claude Code, worker, reviewer and every other run but the coordinator's write the prompt cache
  at `budget.worker_cache_ttl`: `5m` by default (a write at 1.25x input instead of 2x; calls within a
  run are rarely 5 minutes apart), `1h`, or `off` (the agent's own default, which on a Claude
  subscription is 1h). A replay of a day of live runs found 1h cost 11% more for workers and 27% more
  for reviews, and no rule picking 1h at a run's start saved more than 2%, so 5m stays set
  explicitly. The coordinator keeps `coordinator.cache_ttl`. Codex and Cursor have no such switch.
- Long command output stays out of a worker's context: `ttp clip -- <cmd>` and `ttp checks` keep
  it in a file and show a test run's failures (else head and tail). On Claude Code, Bash output
  past `budget.bash_output_max_chars` also goes to a file. A worker compacts its own context inside
  its run (`budget.compact_window_tokens`) and is never ended or restarted because its context grew.
  `budget.split_reread_tokens` is off (0) by default; a project that sets it tells a worker whose
  run re-reads more than that once to hand the rest on as a follow-up; its run note records when, and `ttp stats` shows what steered runs re-read after it and how they handed off.
- Every worker and reviewer run starts from the coordinator's context (`coordinator.session.worker_context`,
  default `fork`). It continues a copy of the coordinator's saved session (written within
  `worker_fork_max_age_h` hours) at any size, with its own prompt as the first new turn, and compacts in
  place inside that session if it must; `worker_fork_max_tokens` above 0 caps the size (default 0: no
  cap). The copy works where the run's provider saves sessions in the coordinator's format: Claude Code
  and the chat provider (which runs Claude Code) fork a Claude Code coordinator's session, Codex a Codex
  coordinator's rollout. The coordinator's own session is never written. Otherwise, and always on Cursor,
  the prompt leads with the coordinator's context on disk: the open order ledger, its latest session
  note and the last chat exchanges (`worker_chat_chars`). `disk` skips the fork, `off` both. A follow-up
  (a task labelled `continues:<id>`: a continue hop, a daemon review fix, a rebase, a re-review) first
  resumes a copy of the session of its own last ok run, or before it has one, of the last ok run of the
  task it carries on, with the coordinator's context since that run (open orders, its latest note, the
  chat since) leading its prompt; that session is never written. A first review continues no task, so
  it stays independent of its author's session. Where that session cannot be copied (another
  provider's format, a missing transcript, an earlier rejected copy), the run takes the coordinator
  fork, then the disk block. The run note's `coord_context` says how a run started (`fork` with the
  provider it copied `from`, `resume` naming its source `run`, `disk` or `none`), so the fork rate
  counts per provider, and `ttp stats --coord-session` shows the share of each per day and how many
  follow-ups resumed. Workers run independent parts in
  parallel subagents; their spend counts in the run's cost.
- `ttp killscan <script>` flags kills by name or pattern (`pkill`, `killall`, any `pgrep` or
  `pidof`, `ps | grep` feeding a kill) before a worker runs a script; `--shim <dir>` writes
  stand-ins for `pkill`, `killall`, `pgrep` and `pidof` that only log the call (`ps | grep` and calls
  by absolute path must be edited out). `pkill -P <pid>` is not flagged. A pattern kill in a script
  can match the worker's own tool shell.
- The worker hook's kill guard refuses a Bash call with `pkill` or `killall` (unless scoped with `-P`,
  `-g` or `-s` to the task's own processes) and `kill` of a pid or group outside the task's own
  process tree, also through wrappers, `sh -c` and ssh; it is always on. See `runtime/ttp/killguard.py`.
  It also passes a kill of the group (or leading pid) of a `ttp detach` job an earlier run of the same
  task started, once four records agree: the project db names the caller's run and the job's run as runs
  of that task, the job's run registry names that pid with the start time the live process has now (a
  reused pid never matches), and the process's own environment names this project, that run folder and
  that job's `.rc`. A file a worker or project writes (an owner or pgid file under the project's state)
  is never proof: a project whose own scripts start long-running jobs starts them through `ttp detach`
  to get this record, and `ttp detach --list` shows a task's jobs across its runs.
- No installs into the shared user site (`guards.user_site`, `auto` by default; `on` also holds it on a
  host of one project): on a host whose `ttp list` shows more than one project, every project imports the
  same `~/.local`, so one `pip install --user` changes every project's packages. There every worker,
  reviewer and coordinator run, and the checks the daemon starts, get `PIP_REQUIRE_VIRTUALENV=1`,
  `PIP_USER=0` and `PIP_BREAK_SYSTEM_PACKAGES=0`, so pip itself refuses an install outside a virtualenv
  on every provider, scripts included. The worker hook also refuses `pip install --user` (pip, pip3,
  `python3 -m pip`, `uv pip --system`, `setup.py install --user`, through wrappers, `sh -c`, eval,
  `find -exec`, xargs and parallel), `python3 -m ensurepip --user` (ensurepip drops the `PIP_*`
  variables), `--break-system-packages`, `--isolated`, a `--target`, `--prefix` or `--root` (or
  `PIP_TARGET`, `PIP_PREFIX`, `PIP_ROOT`) under `.local`, a bare pip install under `sudo` or `env -i`,
  and unsetting or overriding those variables or the same `pip config` names;
  a pip command that installs nothing (download, list, show, freeze, wheel) may carry its own
  `PIP_REQUIRE_VIRTUALENV=0`. Each refusal is counted on one queued ride-along `user_site_install`
  event per run for the coordinator, never a user alert. Install into a venv the project owns:
  `python3 -m venv <dir>` or `uv venv <dir>`, then `<dir>/bin/python -m pip install ...`. See
  `runtime/ttp/usersite.py`.
- Which project owns a process: every worker, daemon-run command, `ttp detach` job and `ttp checks`
  command carries `TTP_PROJECT` (the full path of `<project root>/tt-project`) and `TTP_RUN_DIR` (a
  run or check-run folder inside it). A project's reaper or cleanup script signals only processes it
  can prove are its own: that marker, or its own run record with the pid and its start time. Never pick
  one by a task or run id in its environment, command line, run folder name or log: ids are numbered
  per project and overlap across projects on one host, and every project's folder is named
  `tt-project`. It also skips a process still writing output. The harness's lock reaper and its stop of
  a cancelled task's checks follow this rule (`runtime/ttp/procowner.py`).
- `ttp svcscan <script>` flags service unit changes before a worker runs a driver on a shared
  machine: any `systemctl` verb but a read-only query (`stop`, `mask`, `reboot`, `daemon-reload`,
  `set-property`, ... also under `sudo -n`, in Python argv lists and across backslash-continued
  lines) and `service <unit> stop|reload|...`. `--shim <dir>` writes stand-ins for `systemctl`,
  `service` and `sudo` that only log a change (only read-only queries such as `systemctl is-active`
  reach the real one; `sudo` runs nothing else). Calls by path (`/usr/bin/systemctl`,
  `./systemctl`) or over ssh are flagged as such: a local shim does not stop them.
- Resuming a lost run's session works on Claude Code and Codex; Cursor starts fresh.
- A laptop pauses while it sleeps. Use an always-on machine for round-the-clock work.
