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
- **Coordinator**: by default (`coordinator.session.mode` on, Claude) the project's hands-on
  manager: one persistent session, woken by chat, task ends and timers, that corrects workers
  mid-run and fixes the project's own prompts, scripts and config, while heavy work still goes to
  workers. With the mode off, on an agent without sessions, or while a session cannot run, it is a
  short, tool-less stateless decision over a digest of the project, batched and rate capped, and
  anything needing files, commands or deep thought becomes a task. Either way it reads orders by their intent (a Restriction binds
  as written), decides on its own unless a step could hurt people or speak or pass as a human,
  and asks rarely, never waiting on the answer.
- **Workers**: one task each, in a git worktree on their own branch for code, with a dollar
  budget, a wall clock, and a stall guard. Each hands off a structured result. A worker whose
  machine or queue is busy hands the task back to retry later, instead of waiting inside the run.
  The wall clock and stall guard count only time the host is awake. A run a host sleep cut short
  is retried without spending an attempt and is not counted as waste. After a wake, nothing new
  starts until the host has been awake for `budget.wake_settle_s` (default 300 s), so a laptop's
  brief maintenance wakes start nothing. Messages from people are still answered at once.
  A run a reboot, a sleep or a lost supervisor cut short after real progress
  (`budget.resume_lost`, default $0.50 or 10 min) continues its agent session in the same
  working directory with a short prompt, instead of starting over.
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
- Usage-billed accounts: $100 per 24 hours and $200 per 7 days per project by default
  (`budget.daily_usd`, `budget.weekly_usd`). With `budget.day_start` set, the daily cap counts
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
  from the account settings only). `ttp config --account KEY` reads one; an
  empty value removes it. The same holds for a project's own keys: `ttp config <name> KEY ''` removes KEY
  from `project.json`, so its default applies again, and a key whose default is a list or an object
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
  Its coordinator signal (self-started coordinator turns past `budget.hourly_coordinator_usd`, or
  too many of them on a plan) defers those turns and holds routine work only: turns for your
  messages, urgent tasks and user-ordered tasks (answering a chat, owning an obligation) still start,
  under the dollar caps. `ttp status` says so in its slots line.
- A coordinator turn that goes over `coordinator.turn_budget_usd` is never retried as is: its batch is
  halved down to single items; a single item runs once more alone at the ceiling
  (`coordinator.turn_budget_ceiling_usd`, 0: twice the cap, never above the hourly coordinator limit
  or the dollar caps). One that fails even then is parked and you get one alert naming every parked
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
  updated within `review.unreviewed_max_age_h` (default 72); it runs every 5 minutes and in the
  blocker sweep, reads local refs only, and never queues a second review of one head. A code task still in review
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
  the coordinator, so the same head is never re-reviewed (at most two
  rounds per stack; a failure without fix follow-ups still blocks them, and `upstream:` notes and
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
  whether from its own daemon or relayed by another project's.
- `coordinator.session` sets the persistent coordinator session (on by default): with `mode` `on`
  (Claude), each coordinator wake resumes one agent session instead of a fresh turn; `off` runs
  every wake as the stateless turn; `shadow` is reserved. A project with no value of its own
  follows the default, so new and upgraded projects run the session; a user turns it off for their
  own project with `ttp config <name> coordinator.session.mode off` (kept across upgrades). A
  project's coordinator may set the mode itself (`config_set`). A new session starts
  when there is none, the system prompt, stable context or action schema changed, the model or
  effort changed, the context reached its limit, the last session turn did not end ok, or its
  transcript is gone (a host move). The context is the last model call's prompt and answer; the
  limit is the session's first prompt plus `rollover_growth_tokens` (100000), at most the model's
  context window (`context_window_tokens`; 0 reads it from a `[1m]` model id, else no cap) less
  `rollover_margin_tokens` (40000). `rollover_tokens` above 0 (default 0) sets a fixed limit instead. A resumed wake that fails is rerun at once as a
  stateless turn, so each message and event is handled once; `max_failures_per_h` (3) failed resumes
  within an hour run every turn stateless for `pause_h` (6) hours and raise an issue. While the
  session spend guard has tripped, turns are stateless. A coordinator on a provider that runs no
  session (only Claude Code does) keeps stateless turns, with no spend guard and no session line in
  `ttp status`. `ttp status --verbose` shows the session's
  short id, wakes, context size and last decision. `ttp stats <name> --coord-session` reports each
  day's wakes, $/wake, context growth and tool calls per wake, rollovers and cache-read share beside
  the stateless baseline (the 7 days before the first session wake: turns/day, $/turn), and flags
  the break-even (at most 2 tool calls and 20k context growth per wake) and a day costing more
  than the baseline's $/day. In session mode the system prompt is the
  Restrictions, the coordinator role with `prompts/coordinator-session.md` and the charter verbatim:
  the same bytes every wake while the charter is unchanged, so the prompt cache holds. Once the
  context reaches its limit, one last resumed wake also returns a `session_note` (at most
  4,000 tokens: open threads, promises in their chat wording, pending checks, decisions with their
  reasons, user corrections), saved as `state/coord-session/<session id>.md`. The next session's
  first message carries the standing memory, the open order ledger, that handover note and the last
  20 chat exchanges verbatim, then the full digest. The agent's own auto-compaction is only a
  backstop. A resumed wake sends only the digest sections
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
  A new session, a daemon restart, a stateless, failed or
  compacted wake since, or the action `{"type": "noop", "key": "full_digest"}` sends the whole
  digest on the next wake. A session wake's answer is checked before its turn ends: an action the
  daemon would refuse (an unknown task id, a dependency that failed, a task cap reached, a field a
  running or finished task keeps) comes back as the answer tool's result, so the coordinator fixes
  or drops it in the same turn. An action resent unchanged goes through and is refused after the
  turn as before; at most 2 answers a wake are sent back.
  `tools` (`full`, the default, or `read_only`) sets what a session wake may use (coordtools.py).
  With `full` it gets a worker's tools (Bash, Read, Edit, Write, Skill, ToolSearch and the web
  tools unless `no_internet`) under a worker's PreToolUse checks, so it can edit, build, ssh to the
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
  coordinator run holds no device (`TTP_NO_DEVICE=1`), so the worker device-run rule binds it: a
  device test run outside `ttp sandbox` is denied with the sandboxed form as its hint, never
  rewritten as a worker's is, since heavy device work goes to a worker task. A
  foreground command stops at `tool_timeout_s` (300, at most 600); a longer or background one is
  denied with a pointer to `ttp detach` or a worker task. Each mutating call (the command or file,
  its exit code) goes to the run's `tool-audit.jsonl` and `progress.md`, which the web run view
  shows. Before a fix the coordinator runs `ttp audit why "<why>" --serves <o12|msg#123|#456> --undo
  "<how>"`; each later mutating call in that wake carries the why, the order or task it serves and
  the undo (coordfix.py). A call with none is flagged once in the next digest, the daily review lists
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
  `ttp throughput`; an urgent wait the check above already raised is not raised twice.
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
  desktop, so a server never gets it); hosts that are down are named with no advice. When every
  failing host is already known down (a `ttp machines outage`, a down or held condition, a paused
  resource, an active mute, an open incident or a task naming it as down or under repair), the
  alert is a low feed line, never "needs you". When other queued tasks can still run later, it also
  counts the ones that wait on their own timers or checks.
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
  `(1 machine stale)` when a machine's answer is old.

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
   until `providers.chat.tiers.<tier>.model` changes or the daemon's model-free /models check (at
   most hourly while a tier routes) lists the model again. That check stores only the served ids and
   its time in the project state folder; `ttp status` and `ttp doctor` show them and any hold. Routed runs take worker slots under the `chat` gate (its
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
   a short `cause`: an urgent or user-ordered task (landing in minutes outweighs the saving), a
   review fix or failed-checks wake of a change its lineage wrote on the routed provider, and every
   later run of a lineage with two routed misses (routed runs that fell back on their own or handed
   off `failed`, and routed changes a review or failed checks sent back). The `routed 24h:` phrase
   counts them apart (`2 escalated to core`) and `ttp stats --runs` ends with their count by kind.
   `"route_timeout_s"` next to a routed tier's `model` sets the time limit of its routed runs in
   place of the tier's `budget.run_timeout_s`; a start on the core provider keeps the tier default.
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
`providers_allowed` and not the core provider. Each run note has a `dispatch` entry (by, pick, p); a Jev pick is logged
with the stratum's mean core cost per run over 7 days as its avoided cost and settles right when its
task ends done on its first try with no fallback, wrong when it escalates; a core pick counts as no
change. Jev's usual review switches the use off when it saves nothing or changes no decision.

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
  owner only while the gate allows a slot. A red gate (the coordinator turn guard's red holds routine
  work only), the dollar caps, the auth gate, pauses and locks still hold it.
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
  or deploy still goes first.
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

## Parallel work

- Up to 6 workers per project run side by side (`budget.max_parallel_workers`); on a plan, all of
  them until the last stretch before the line. When a plan is below its line, slots sit idle and
  nothing is queued, the coordinator is asked for more work, less often each time it finds none.
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
- On macOS, while the project has work (a running run or push, or queued work that can start now)
  and the machine is on AC power, the daemon keeps it from idle and maintenance sleep with
  `caffeinate -i -s`, tied to the daemon's process so it ends with it. A closed lid without an
  external display, or a sleep you ask for, still sleeps the machine; the harness never changes
  `pmset` settings. It lets go when the work ends, on battery, or with `runner.prevent_idle_sleep` set
  to `off` (`auto`, the default, is on for a Mac; other platforms never hold one). `ttp status` and
  the web app say when it is held, or why not while there is work, and say "the lid is closed: work
  pauses while the Mac sleeps" while a closed lid will sleep it.
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
  task (from any of its runs; its .lost and stop event name the caller's task and run, the run that
  started the job and the reason), or one holding a resource of the calling open priority-1 task;
  anyone else is refused (exit 3). A stopped job is never relaunched at boot. Such a stop (also a spend
  cap's, or the daemon's of an ended task's job) writes `<name>.rc` holding 137 (128 + SIGKILL) beside its
  `<name>.lost` unless the job wrote its own, so a reader of the .rc alone sees an ended job, not a dead
  one; `ttp detach --check` prints `stopped on purpose rc=137 (<why>)` and the closed record is `ended`
  with `stopped` and the why.
  anyone else is refused (exit 3). A stopped job is never relaunched at boot.
  A push check or `ttp checks` command that runs `ttp detach` inside the check's process sandbox (a
  PID namespace that ends with the check) has its job queued: the daemon starts it outside within
  seconds, with the check's device masking (`TTP_NO_DEVICE`, `TT_VISIBLE_DEVICES`, hidden device
  nodes) and without the check's own fds and reaper token, and its .rc and .log stay in the check's
  report folder. No task owns such a job, so the task sweep and leftover stops never end it; its
  owner is the push or checks run named in the check allocation's `owner.json`. The daemon ends it
  6 hours after it was queued or once that owner's marker file is gone, whichever comes first, with
  the reason in its .lost; put a tighter bound in the command itself (`timeout`).
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
  SIGKILL if it does not exit (0 stopped or already gone, 2 none found: it names where it looked). `ttp detach --remote <ssh alias> [--dir <dir>] <name> -- <command>`
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
  The limit counts wall time since the check started, less only time it spends waiting for a
  grant (a `ttp lock`, a checks lane or a host-wide checks slot): waits for a test-pool slot
  count. Past it the check is ended within a few seconds, running or waiting for a slot, and the
  push log says it timed out, after how long and how much of that was pool wait. A check still
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
  check passes or a batch pushes. It never counts as passed.
  Any test verdict in the output keeps a failure a failure. A gate of your own that knows it ran
  nothing can say so directly: exit 75. A job a check detaches makes its work dir inside the job,
  after it is detached, so it outlives the check (see `ttp detach` above).
  A check script that runs its own test pool and is granted fewer slots than it planned asks
  `ttp pool-admit --planned N --granted K [--files F] [--what <pool>]` before it starts the shards:
  exit 0 starts them; exit 3 means the run on K slots is estimated to go past the time the check
  has left, so it is not started there, and stdout gives the fewest slots that would fit. The
  script then gives the slots back and waits for that many inside `ttp pool-wait` (that wait
  counts against the limit, so it asks again once granted), or runs the set on this host's checks
  slots (`ttp checks-shard`). The estimate is the median slot-seconds per file (with `--files`),
  else per planned shard, of earlier passing runs the script recorded with `ttp pool-admit --ran
  <s> --granted K [--planned N] [--files F]`; with none, each planned shard is taken as half of
  the whole limit. Granted every planned slot, or with no limit known, nothing changes. A push
  check gets its limit in `TTP_CHECK_LIMIT` (the time left takes off only its grant waits, as the
  push does), a `ttp checks` run its wall cap in `TTP_CHECKS_WALL`.
  Why this design: refusing the cut run is the simplest rule that never starts a run certain to
  time out, and it leaves the project's own pool in charge of where its shards go. The limit is
  never scaled for a cut run: that would let one run hold a push longer than the project set.
  A gate that runs its test files on a pool of hosts starts each shard as `ttp pool-shard --shard <id>
  --pool <alias> [--pool <alias>...] [--cancel <cmd>] -- <cmd...>` (`{pool}` and `{shard}` in the
  command are filled in; TTP_POOL and TTP_POOL_SHARD are set). The shard prints one line per file
  done (`PASS`/`FAIL` at the start, or pytest's `[ NN%]` progress; `--done-re` changes it). Its time
  before the first file (a queued batch job, setup) is a test-pool wait: the push log shows it and
  `delivery.push_pool_wait_s` bounds it, and like any pool wait it counts against the check's limit. A shard with
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
  rescoping and a coordinator may set it itself. Only passes the push or the push queue made carry,
  never passes recorded by `ttp checks`.
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
  `checks.dirty_max_mb` (default 2048) or `full avg60` in /proc/pressure/io is over `checks.io_full_max`
  percent (default 10; a kernel without PSI skips only this signal). Both are account settings
  (`ttp config --account checks.dirty_max_mb N`, or `TTP_CHECKS_DIRTY_MAX_MB` / `TTP_CHECKS_IO_FULL_MAX`;
  0 turns that signal off). A run that may spill then tries the spill hosts first, else it keeps its
  place and waits; running checks and the push branch's checks (its priority slot) go on. `ttp checks --slot-status` shows the values
  and the reason. A set cap wins: `ttp config
  --account checks.host_max N` (0 turns the cap off) or `TTP_CHECKS_HOST_MAX`; it is one value for every
  project (a project's own copy is ignored and `ttp config checks.host_max` shows the account's). While
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
  one per 8 cores, at least 2, plus the cores the larger of the 1- and 5-minute load, less one per test slot the project itself holds, leaves under
  0.75 x the cores, at most half the cores, so its own suites never shrink it; `checks.load_extra` 0 keeps the base; a shrink bounds new grants only), the most test workers one project's suites run at once, which also bounds one run's
  grant. No new suite starts while the host's 1-minute load is above its cores (`checks.load_guard`, a factor of the
  cores, default 1; 0 turns it off): it waits and its lane's wait note says why; running suites go on. Push
  checks wait there at most `checks.push_load_wait_s` seconds (default 300; `off`: no bound), then start on
  their branch's reserved lane alone, one per lane at a time. Heavy suites
  belong on another host where a project has one allowed (`delivery.checks_host`).
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
  in an extra lane past the cap, one such lane per project at a time; it never stops a running check. Unset, `checks.slots` (chat-settable, a whole number; `checks.suite_concurrency` is its chat-settable
  alias) is the number of runs the host's test-slot pool fits side by side, at least 2 and at most 4, and lower only
  when available memory is under 8 GiB per lane. Runs at once do not collide: each has its own clean checkout,
  TMPDIR, output folder and block of ports (`TTP_PORT_BASE`, `TTP_PORT_COUNT`, for suites that bind fixed ports);
  checks that run in the authoring worktree itself also hold that worktree's one lane. On Linux a run's clean checkout
  and TMPDIR go on the RAM disk (`/dev/shm`) when it has room for this run's scratch (the checkout's real size,
  submodules included, times 1.5 plus 2 GiB of temp files, at most a quarter of the disk; 4 GiB counted per run
  already there) while keeping a quarter of it free and a fifth of the RAM available, else under `/tmp`, as are
  checks that build (cmake, make, ninja): concurrent suites writing their temp git repositories to a disk filesystem wait on its locks. A lane goes to the most urgent
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
  else by kind) takes an extra lane beside them. A waiter says who holds the lanes and its place in the queue
  each minute; `ttp status` and the coordinator's digest list waits over 10 min, and the daemon records one
  `checks_slot_wait` event per wait past `checks.wait_event_s` (default 900, 0 turns it off). Only one of them
  wakes the coordinator per queue incident (from the first waiter past that threshold until none is); the
  rest are recorded `coalesced` and show in the digest and `ttp status`. The incident wakes it again only when
  a decision is due: its longest wait passes `checks.wait_escalate_s` (default 7200, then once per as long
  again; 0 never), a holder has run `checks.wait_hang_factor` (default 3, 0 never) times the recent median
  checks run (a likely hang), or a holder's task has ended (a review precheck: once no review reading it is open). An `observation_mute` on source
  `daemon:checks_slot_wait` covers these events too (recorded `muted`); coalescing already keeps a long queue
  to one wake, so a mute is only for a queue the coordinator has already decided to leave alone.
  `delivery.checks_spill_hosts` (an ordered list of ssh aliases, default empty: checks never leave this machine)
  lets a run that has waited `delivery.checks_spill_after_s` (default 60 s) here run on the first listed host the
  charter names, the machines list holds, no pause covers and whose `ttp lock <alias>` nobody else holds (the run
  holds it shared while it runs), where no device job runs, that ssh reaches and that has a free slot; others
  are skipped with the reason and the run keeps its place here. A device job means one of the host's device
  locks (`<alias>-device`, the device names of its machines-list `shared` entry, `device.locks` names on it)
  is held, reserved or waited for (a pool check run's own hold of the host's lock is none, even when `device.locks`
  lists the bare alias), or a broker heal check (preset broker) of the project on that machine
  reports a running job row; a broker status that cannot be read never keeps a run off. Runs already there go
  on, and `ttp status` and the web app show the skip in one line with the host and the reason.
  `checks.spill_beside_device` (ssh aliases, or `"*"` for every spill host; default none) names hosts where the
  project's own rules let its CPU-only checks run beside a device job: there the device job keeps no run off, a
  pause or a held machine lock still does, and the run and its `ttp status` line say it runs beside a device job
  (`ttp config <project> checks.spill_beside_device '["host-a"]'`). The account's pool, `checks.spill_hosts`
  (`ttp config --account checks.spill_hosts '["pool-a", "pool-b"]'`; only the user sets it, a run may only
  clear it with `[]`), follows each project's own list, so every project on the machine spills to it without a
  list of its own; its hosts need no charter line but must be in the machines list. A project's own `[]` keeps
  its runs here, pool included. Runs with a check host or the authoring tree never spill; runs behind
  `checks.wrap_cmd` spill only when the project sets `checks.wrap_needs_device: false` (they run there without
  the wrapper; unset or true, the run stays here). Two settings drop the wrapper, for different cases.
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
  runs unwrapped. The order when the slots here are full (the project's lanes or the host-wide checks cap): an
  urgent run asks the spill hosts at once, others after `delivery.checks_spill_after_s`; the first listed host
  that is allowed, free and reachable takes the run; only when every one is full, paused or unreachable does it
  wait here (and try them again after the same bound). A run that does not pass on a spill host runs again in
  the same order, keeping off the hosts its checks failed on (one whose own fault ended them, such as a pause
  taken mid-run, gets one more try once free); a pass on another spill host counts, else the result here does.
  `checks.json` (`spilled_to`), `ttp status` and `ttp checks status` name the host.
  `checks.slurm_overflow` (account-level, unset by default: nothing changes) sends a run that no pool host can take
  (each paused, busy with a device job, full or unreachable, or no pool at all) to a Slurm cluster instead of
  waiting here: `ttp config --account checks.slurm_overflow '{"host": "<cluster-login>", "partition":
  "<cpu-partition>", "workdir": "~/ttp-checks"}'` (only the user sets it, a run may only clear it with `""`).
  `host` is the submit host's ssh alias, `partition` a CPU partition, `workdir` a folder on the cluster's shared
  file system; `max_jobs` (2) caps this machine's jobs there at once, `--time` is the run's median wall time plus
  `margin_s` (600), at most `max_time_s` (7200, also its value before a median exists), and a job pending past
  `max_pending_s` (1800) is cancelled. Each check is one batch job (`--nodes=1 --ntasks=1`, no GRES) on a copy
  of the commit, copied as for a spill host; the submit host only runs sbatch, squeue, sacct and scancel and
  serves the job's output and exit code. A run with a spill wrapper (`spill_wrap_cmd`) never overflows: the
  batch job runs none. Urgent runs take a free job slot first. A cluster ssh does not reach
  keeps the run waiting here; one whose sbatch refuses the job, or a job that ends for a cluster reason twice
  (it is submitted again once: never started, node fault, preempted), is an environment fault and the run runs
  again here, whose result counts. Stopping the run (a stop, a timeout, a newer head) cancels its job; a job
  whose run died is cancelled by the next overflow pick, and one the submit host stopped answering for still
  ends at its time limit. Every job (id, project, task, check, submit, start and end times, state, exit code) is
  logged in `check-slots/slurm/jobs.jsonl` under the account folder; `ttp status`, `ttp checks status` and
  `checks.json` (`spilled_to`, the submit host) show running ones like a spill host's. A project's own
  `delivery.checks_spill_hosts: []` keeps its runs here, overflow included.
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
  the tt-project test suite lists its 15 slowest test calls just above its `ttp-suite:` line. `checks.wrap_cmd` (project setting, default unset) runs each command given after `ttp checks --`
  behind the project's own lock or queue command, the command passed as separate arguments; a missing wrapper
  fails the checks, and `delivery.push_checks` never go through it. A wrapper running its own test pool starts
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
  the grant is lowered with a line saying so; a worker's own pytest above TTP_TEST_SLOTS is refused. Builds
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
  they never queue behind task checks: each push branch has one priority slot of its own beyond the
  cap, shared host-wide across projects. Task checks keep their cap and their order. The batch log
  and `ttp status` say when a push holds it (`push priority slot: ...`); a killed push frees it too.
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
  runs are never held by a less urgent one. An urgent run (priority 0 or 1: a task owed to the user,
  live-impact work) that would wait with every slot busy starts within about a minute, in this order:
  on a free host of the account's test pool at once (no `checks_spill_after_s` wait first; a paused
  host is never picked); else on a reserved urgent slot beyond the cap, with a reserved test lane past
  its project's cap (`checks.urgent_slots`, account-level, env `TTP_CHECKS_URGENT_SLOTS`, default 1, so
  at most that many urgent runs above the cap at once; 0 turns them off); else, once it has waited
  `checks.preempt_after_s` from its arrival (account-level, env `TTP_CHECKS_PREEMPT_AFTER_S`, default
  120; 0 asks at once), it asks one routine run on a cap slot to step back, once per request: a run of
  its own project first, then the least urgent, then the latest started. That run ends its current
  command, frees its slots and queues again at its old arrival, ahead of every later arrival of its
  project, keeping every pass and log it recorded (a pinned run keeps only passes on its pinned commit
  and reruns the rest there), and the urgent run takes the freed slot. Its log, its task's notes and
  its queue entry in `ttp status` say `preempted by urgent #<task>`, and its own rerun goes on to its
  result. Another urgent run, a push check and a run behind a lock (the `checks.wrap_cmd` wrapper, or
  commands that run `ttp lock`, `ttp devq` or `flock`) are never asked. A routine run whose task
  turns urgent while it waits notices within 30 s and re-executes once as an urgent run, at the same
  arrival. `ttp status` shows `urgent slot beyond the cap: ...` while one runs. While any run waits for test slots, push
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
  failing again once on the merge base with the push target (each rerun bounded by
  `checks.judge_rerun_s`, default 600). Failing on both is pre-existing and does not block (reported by
  name); passing on the head rerun is flaky: it goes into the project's quarantine
  (`state/test_quarantine.json`: first seen, evidence, expiry after `checks.quarantine_days`, default 7),
  which `ttp status` shows, one fix note goes to the coordinator, and it does not block. Failing on the
  head rerun and passing on (or missing from) the base blocks, as does a rerun that cannot tell. A saved
  base result counts only when it ran on the same host for the same base commit. A quarantined test the
  change does not touch is excused without a rerun. Nothing in the repository is edited or skipped. An
  excused check counts for this change's landing only: the run reads `LANDING ONLY`, its pass is never
  recorded for reuse, and `ttp checks --release` (a release cut's or a required whole-suite check)
  excuses nothing and joins no other run. `checks.judge: false` turns judging off.
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
  pass: it never counts for a PR or `on_pass`. `ttp checks --full` runs the project's checks
  anyway (it is kept when the checks are queued or relaunched after a reboot), and a full pass
  already recorded for the tree is reused. A scoped `push_checks` entry that applies also turns it
  off. Known gap: a project that opts in owns its globs. A glob that covers installed or
  test-asserted content (for example a plugin README that tests read) skips the checks those
  changes need; list only paths nothing installs or tests.
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
  reworded or deleted, each line as its side wrote it), and edits of different cells of one Markdown
  table row. A conflict only in the shared release files (the version files,
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
  review and wakes the coordinator. The batch finds the first change that fails by checking
  prefixes, pushes the ones before it, and checks the ones after it once more without it, pushing
  them too when they pass. A failed pytest check first reruns only its failing tests once at the
  same head (at most 10, each once per batch, within `push_rerun_s`, default 600): when they pass
  the failure was flaky, the batch goes on and the coordinator is told once per test so a
  deterministic fix gets queued; a test that fails twice never passes. A check that did not exit 1,
  or that pytest stopped early (`-x`, `--maxfail`, an interrupt), is never rerun. When the first change is
  blamed, its failing tests are rerun once on the tip alone: failing there too, the tip is reported
  broken instead of the change. Keys, all under `delivery`: `push_batch_s` (default 300: with
  no review running or ready an approval starts a batch at once; otherwise the batch starts once the
  oldest approval waited this long; 0 = at once), `push_batch_max`
  (default 8: start at once with this many; also the most one batch takes, except that after a
  clean batch a bigger backlog grows the batch up to three times this), `push_min_gap_s`
  (default 1800: after a batch that did not end clean, such as a failing tip or a dead batch, the
  next starts only once it ended at least this long ago, measured
  from its recorded end so a restart never stretches the wait; none after a clean batch, though a
  new batch never starts while the last one's after_push still runs; a full batch, a
  priority 1-2 review's approval, or one the last batch requeued unchecked after a failing change
  starts at once; 0 = off). Approvals that waited while the last clean batch ran start the next one
  at once, and priority 1-2 approvals always lead it. Within a priority, an approval an earlier batch
  sent back as a conflict leads, so a peer ahead of it cannot send it back a second time. `push_solo_paths` (default `[]`, off:
  git pathspecs a batch takes at most one routine approved change touching; the oldest such change
  leads the next batch and priority 1-2 ones are never held. Two prompt changes that fit a size cap
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
  for batches held because one check failed before any test ran, after its reruns), `after_push`
  (commands in the same form as `push_checks`, for example a deploy) and `after_push_timeout_s`
  (default 1800). A batch that ends without writing an outcome is retried with a growing pause,
  and three such batches in a row raise a high alert, unless the host rebooted while it ran: then
  it is reported as cut short by that reboot (its time in the home zone), counts no try and no
  pause, raises no alert by itself and runs again as soon as the daemon is back. A push the remote refuses because the token lacks a scope (GitHub: `.github/workflows/*` without `workflow`) holds only the changes that need it, pushes the rest at once, raises one `push_scope:<scope>` alert and puts the held changes back once `push_scope_check` passes (default a `gh auth status` scope check, every `push_scope_probe_s`, default 600). `after_push` runs after each push in a clean worktree at the pushed commit,
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
  own daemon reports the release (output in `logs/takeover.log`). A live daemon
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
  document), Codex connector tools (its hook gets Bash calls only) and Cursor runs (no harness hook).
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
| `review.sweep_after_s` | 300 s (old: 1800) | a code task left in review before its review is queued | convenience |
| `review.unreviewed_wait_s` | 1800 s | a code task waiting externally (a busy lock too) before the backstop reviews its head | convenience |
| review sweep cadence (`REVIEW_SWEEP_EVERY_S`) | 300 s | how often both review sweeps look | convenience |
| review settle grace (`UNREVIEWED_GRACE_S`) | 120 s | a task updated this recently before the backstop sweep looks at it | convenience |
| `coordinator.review_stall_s` | 1800 s (old: 14400) | a dependency in review before the coordinator hears of it | convenience |
| `checks.test_project_max` | from cores and memory, at least 2 (`auto`) | a project's suites at once | convenience |
| `delivery.push_batch_s` | 300 s, none after a clean batch | changes gathered into one push | convenience |
| `delivery.push_batch_max` | 8 | most changes in one push batch (a full batch starts at once; a backlog after a clean batch grows it up to 3x) | convenience |
| `coordinator.burst_window_s` | 120 s (0: off) | high events after a turn high events woke, until one follow-up turn reads them all; never user messages, critical or safety-class events | convenience |
| `retry_when` / `start_when` probe (`waiting.poll_s`) | 90 s (20-180 s); only a `start_when` probe of an arbitrary command that keeps saying not-yet backs off, to at most 600 s (`MAX_EVERY_S`) with the wait's length; landing (`landed:#`, ttp landed), job (detach --check), checks --result, push --result, lock --probe, CI, ack-wait, upstream --fixed and date (`test "$(date ...`) probes, and every `retry_when` probe, keep `waiting.poll_s`; a pass wakes the task at once whatever its timer | a waiting or deferred task before its probe runs again | convenience |
| auth re-probe (`AUTH_PROBE_S`) | 900 s | work for a logged-out provider with no login check, before one run checks it | safety (spend) |
| `ttp lock` on a device | one command at a time | device commands | safety |
| suite lock | one full suite per worktree | a second run of the same suite | safety |
| `checks.host_max` | from the host's cores and load | suites on one host | safety |
| `delivery.push_min_gap_s` | 1800 s, only after a batch that did not end clean | the next push | safety |
| `budget.max_parallel_workers` | 6 | worker runs at once | safety (spend) |

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
same leading key, or most of the text alike), goes to a harness task, at most one per
`upgrade.task_every_h` (6) hours; releases meanwhile wait and the next task merges the newest.
The same conflict deferred past 48 h, or past two harness tasks that ended without landing it,
reaches the coordinator once as a normal observation, not once per deploy.

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
- Workers run with your permissions, in their own worktree; restrictions in the charter are
  passed to every worker and enforced by the provider where it can (for example, no web tools).
- When the user loosens or removes a Restrictions item (a quoted edit, or a `replaces` of a whole
  Restrictions section, judged item by item), adds a scoped exception to one (`excepts`, or a
  `both_hold` text that permits what it bans), or `over` retires one, the daemon queues one
  priority-1 harness task per change naming the project's tools (bin/, hooks/, schedule commands,
  prechecks and heal checks, open tasks' `start_when` probes) that still carry the old item's terms,
  and a `restriction_tools` event says so; the digest shows a "Guards behind the charter" line while
  that task is open. It never edits a tool. The charter guard's refusal says a user's loosening
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
- Workers run without a sandbox by default (no bwrap, no network or PID namespace; sudo works).
  `providers.<name>.write_fence: true` turns the provider's write fence on (Claude's Bash sandbox,
  Codex's workspace-write), and `sandbox.checks: true` the process sandbox for checks. A sandboxed
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
- No `ttp` command prints a saved key. `ttp secret check [alias]` tests one (reachable, auth ok or
  auth failed), lists the model ids it serves and marks each `providers.<p>.tiers.*.model` of the
  current project it does not serve, with no key, URL or host in its output, and `ttp secret env <alias> -- <command>` gives it to a command in `OPENAI_API_KEY`
  (URL in `OPENAI_BASE_URL`). A worker's Bash calls that read the secrets store are refused.
- The harness masks every saved secret as `[secret:<name>]` in what it keeps or sends on: the
  database (events, notes, notify text, hand-offs), the daemon log, each run's folder and its agent's
  session transcript once the run ends, and text for Jev and Slack. The secrets store is mode 0600
  and run folders 0700.

## Limits

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
  that copy brings no hooks, MCP servers or apps). A `plugin_dirs` set for
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
  run are rarely 5 minutes apart), `1h`, or `off` (the agent's own default). The coordinator keeps
  `coordinator.cache_ttl`. Codex and Cursor have no such switch.
- Long command output stays out of a worker's context: `ttp clip -- <cmd>` and `ttp checks` keep
  it in a file and show a test run's failures (else head and tail). On Claude Code, Bash output
  past `budget.bash_output_max_chars` also goes to a file. A worker whose run re-reads more than
  `budget.split_reread_tokens` of context is told once to hand the rest on as a follow-up; its run
  note records when, and `ttp stats` shows what steered runs re-read after it and how they handed off.
- `ttp killscan <script>` flags kills by name or pattern (`pkill`, `killall`, any `pgrep` or
  `pidof`, `ps | grep` feeding a kill) before a worker runs a script; `--shim <dir>` writes
  stand-ins for `pkill`, `killall`, `pgrep` and `pidof` that only log the call (`ps | grep` and calls
  by absolute path must be edited out). `pkill -P <pid>` is not flagged. A pattern kill in a script
  can match the worker's own tool shell.
- The worker hook's kill guard refuses a Bash call with `pkill` or `killall` (unless scoped with `-P`,
  `-g` or `-s` to the task's own processes) and `kill` of a pid or group outside the task's own
  process tree, also through wrappers, `sh -c` and ssh; it is always on. See `runtime/ttp/killguard.py`.
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
