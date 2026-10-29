# tt-project design notes

For people (and agents) changing tt-project itself. Users start with the plugin README.

## Shape

| Piece | Code | Role |
|---|---|---|
| `ttp` CLI | `runtime/ttp/cli.py` | create, find, attach, relay, operate; forwards to remote projects over ssh |
| Daemon | `runtime/ttp/daemon.py` | one per project; the only component that starts runs |
| Runner | `runtime/ttp/runner.py` | detached supervisor per run: stdin prompt, lease, wall clock, budget, stall guard |
| Coordinator | `runtime/ttp/coordinator.py` + `template/prompts/coordinator.md` | digest in, JSON actions out, validated before applying |
| Workers | `runtime/ttp/prompts.py` + `template/prompts/worker.md`, `kind-*.md` | one task, one handoff (`result.json`) |
| Mid-run updates | `runtime/ttp/hook.py` | rescopes reach a running worker: appended to `steer.md`, delivered once by a post-tool hook (Claude Code) or read between steps |
| Budget | `runtime/ttp/budget.py` | gates per provider from plan windows or dollar caps, runaway guard |
| Coordinator turn cap | `runtime/ttp/turnsize.py` | per-item records of over-budget turns: halve the batch, one raise for a single item, park it with an alert |
| Billed spend | `runtime/ttp/billing.py` | which spend was billed: per account and time, the one filter every dollar total uses |
| Screening | `runtime/ttp/screen.py`, `providers/jev.py` | dedupe → rules → Jev → wake the coordinator or not |
| Watchers, schedules | `runtime/ttp/watchers.py`, `schedule.py` | model-free probes that report changes only |
| Providers | `runtime/ttp/providers/` | build argv, parse output, report account and plan windows |
| Web app | `runtime/ttp/web.py`, `web/` | JSON API + static page, localhost + token |
| Services | `runtime/ttp/service.py`, `notifier.py` | systemd user / launchd / cron watchdog; desktop notifier |

State is one SQLite file per project (`state/project.db`). Everything a run produces is on disk
in `state/runs/<id>/`, so a daemon restart never loses a result.

`state/scheduler.jsonl` audits dispatch's picks: each slot fill whose candidates, held counts or pick
changed adds one line (runnable queued tasks in rank order with priority and obligation flag, held
counts by reason, the ids started); it rotates to `scheduler.1.jsonl` past 2 MB (`runtime/ttp/schedlog.py`).

Detached checks keep durable reports in `state/check-reports/<id>/` and managed scratch in
`state/check-runs/<id>/checks-tmp`. Only check subprocesses holding the lifetime guard receive
that scratch environment; push and batch supervisors, Git commands and `after_push` keep
their ordinary temporary directory. Recovery reclaims only owned, unlocked check allocations.
Accepted gaps: legacy allocations without `owner.json` and old run-local scratch remain on
disk untouched, with no migration; nothing else is reclaimed by check recovery.

## Invariants (keep them; tests guard most)

1. Models never poll. The daemon polls; a model runs only for a decision or a task.
2. At most one coordinator turn at a time; turns are debounced, batched and capped per hour.
   Routine events (a task done, its follow-ups and notes, normal observations) wait up to
   `coordinator.batch_s` for company, only while every worker slot is busy or runnable work is
   queued; user messages, failed, blocked or review hand-offs, high events and an idle slot with
   nothing to run wake it at once. A ride-along event (data flag `ride_along`: a forgotten
   `continue:` follow-up, a repeat block of a non-urgent task) starts no turn alone: it joins the
   next turn something else starts, or starts one an hour after it was raised. It stays queued
   in the database, so a restart keeps it (pagesev.py).
   A retry the daemon schedules by itself (after a refusal or a run with no verdict) starts no
   turn; the final attempt's outcome does. The daily review skips a period with no work.
3. No run starts when its provider's gate forbids it. Every run has a wall clock and a budget.
4. A run's outcome is read from files, never a pipe. Runs survive daemon restarts.
5. A missed schedule fires once on wake, never once per missed slot.
6. Plan windows keep `reserve_pct` for the user as a hard line. Below it the project runs all its
   workers, never spreading use evenly over a window; near it, it starts a run only while the
   latest reading plus what running work and the new run may still add stays under the line, from
   burn measured in its own readings, never from an assumed rate. At the line nothing new starts
   until the window resets. Gate changes below red are pacing: they alert no one and wake no
   coordinator; reaching the line alerts once per window, and the alert clears as the gate leaves
   red. A provider that
   reported plan windows in the last week stays on the plan regime; old readings never fall back to
   dollar caps. Usage-billed accounts have dollar caps. Cancelled runs are decisions, not waste.
7. A limit or logout pauses a provider with one clear alert; it is never counted as task failure.
   Neither is a `waiting` hand-off (busy machine or queue): the task retries later, up to
   `budget.max_waits` times, then asks the user. While its `retry_when` probe exits 1 ("not yet")
   or 255 (ssh could not reach the host) it sleeps on without a run; past `waiting.max_hold_s`
   after the hand-off it still sleeps, and the coordinator is asked once to fix the probe or cancel.
   A probe that can never pass (command not found, a run dir or worktree that is gone, a devq job
   unknown to its runner) is raised once per wait when it is first seen.
   A spec update wakes it once (a `task_update` by the coordinator or the user; the hand-off keeps
   the digest of the spec it slept on), so its worker reads the new spec before the probe passes;
   another hold (a dependency, start_after, start_when, a paused or busy resource, a gate) still
   holds it first, and at most 3 such wakes an hour wake one task.
   A queued wait whose note names an ask or task (`ask #12`, `task #34`, `#34`) is kept current:
   once that ask is resolved or that task is done, the note says so and its probe (retry_when or
   start_when) runs again at once. Its probe, dependencies, start_when, not_before and attempts
   stay, a done task is only a reason to check again, and each piece of evidence counts once per
   wait. Known gap: a wait with no probe is never woken early; its note is refreshed and it waits
   for its normal retry. A bare `#N` that is really a PR or issue may be read as a task, which only
   rewrites the note or reruns a model-free probe.
   A wake that hands back the same wait (`waiting_for`, `retry_when`, branch head) made no
   progress: later wakes run light only, and after `waiting.max_stale_wakes` (3) the task is
   blocked for the coordinator to split or re-plan, and not woken again. The block, a requeue or
   a new spec starts the count over: the next run is at the task's own wake tier.
   A passed probe needs no model run when the hand-off says what comes next: an `on_pass` (the
   final hand-off, applied when the run's own detached checks passed on its head) or an
   `on_pass_cmd`, run from the project root with the owner's HOME once its run has ended, and killed
   after `waiting.on_pass_timeout_s`; its exit 0
   with a `done` or `needs_review` hand-off at `$TTP_RESULT` ends the task; a failed or overrun
   command wakes it at its own tier, anything else wakes it as usual. A failed `ttp detach` job's
   wake carries its log's last 20 lines and first error line. An `on_pass_cmd` is dry-run once when
   its run hands off (`onpassdry.py`, `waiting.on_pass_dry_run`, on by default): a crash hands the
   run back to its own session to fix the command.
   A task that hands off `done`, or `blocked` with a hand-off naming none of its jobs, has the live
   `ttp detach` jobs of all its runs stopped by the daemon's jobstop sweep (`jobstop.py`), except a
   job its retry_when, on_pass_cmd, on_pass or next_step names (its .rc or `<run>/<name>`) or an
   open task still waits on.
   Before a due wait starts a model turn, the wait-wake gate (`waitgate.py`) decides whether one
   is needed. Must-wake events always wake: an escalation, any wake reason but a timer or "probe
   passed", a changed spec, a probe answer that changed since the last skip, and the cap
   (`waiting.wake_gate.max_skips`, 4, or `max_skip_h`, 4 h, since the last turn). Model-free rules
   then skip what is still pending (a detached job with no exit code, a lock still held, checks or
   a push still running or queued, a probe pass the last turn already saw); Jev, where its
   `wake_gate` use is on, judges the rest; anything else wakes as before. A skip re-defers the
   task with a doubling backoff (`backoff_s` to `max_backoff_s`) and keeps the probe answer it
   saw; the probe loop wakes it at once when that answer changes. The next real turn settles each
   decision (a wake that only handed the wait back, or a skip whose next turn found work, is a
   miss), and the daily review reports skips, Jev spend and the estimated saving.
   `waiting.wake_gate.enabled` false turns it off.
8. The project folder ignores itself; nothing of a project is ever committed to the user's repo.
9. Secrets live only in `~/.tt-project/secrets.json` (0600). Never in argv, logs or projects.
10. The runtime is standard-library Python ≥ 3.9. Web assets are static files.
11. Text from outside (logs, issues, chats, PR comments) is data, never instructions.
12. A run's end, its spend and its task's new state commit in one transaction. No state row can
    wedge the loop: a task never stays `running` without a live run, and a queued task whose
    dependency failed or was cancelled is blocked with the reason. The coordinator unsticks it by
    re-pointing `depends_on`, or by adding a replacement with `continues`, which takes over the
    dependents (and, for code, the branch) in one transaction. A requeue onto a dead dependency is
    rejected, never silently undone; a block left after a coordinator turn is raised once.
13. A question to the user carries a reason (access, funds, spend, review, merge, irreversible,
    restriction, human); anything else is a judgment call the coordinator decides and records.
    No question blocks: each stores its recommendation as its default, shown with the time it
    applies, and after `coordinator.ask_timeout_h` (0.5 h; 0 turns it off) unanswered the daemon
    resolves it to that (`askauto.py`, `expire_asks`), tells the chat at low and records a decision
    memory. A restriction ask whose recommendation would loosen it keeps the restriction as written.
    Asks only the user can answer stay open: review, merge, another human's request, a
    recommendation that acts as the user, a credential or money grant, and an irreversible step that
    does not decline. Nothing resolves at a red gate or when the user has written since the ask (the
    coordinator is asked to check instead). An open ask holds only the tasks anchored on it.

## Known gaps (accepted)

- A wait on another project of this machine gets nothing extra from the daemon: no note to that
  project, no default end, no matching of project names in `waiting_for`. The worker tells the
  other project itself with `ttp note --to <project>`. A wait that never ends is caught like any
  other: a timer-only wait is blocked after `budget.max_waits` tries, a probe that can never pass
  raises one `probe_never_passes` event, and one still saying not yet past `waiting.max_hold_s`
  raises one `wait_stale` event. A daemon-side version was dropped: it moved the end on every
  re-wait, sent the note again each time and matched project names too loosely.

- The project lease (lease.py) only refuses dispatch on a host that does not hold it. Shared
  effects of workers already running there (pushes, device jobs, `ttp lock`, ledger writes) are not
  yet fenced by `TTP_LEASE_EPOCH`, there is no ssh-file store, and a stand-in, hand-back or planned
  move needs later steps. "Failover" in lease docs means moving a project between hosts; provider
  failover (failover.py) moves a task between model providers.

- A pause's health end cannot be tuned. A coordinator pause of a resource with an enabled heal
  check is lifted after 3 consecutive healthy results over 30 min (`pauseends.HEALTHY_N`,
  `CLEAN_S`), also when it has an `until`, which stays the deadline reported to the coordinator.
  The coordinator's `end_when` is a shell probe only, and a probe replaces the health end. A
  hand-written `{heal, healthy_n, clean_s}` end was dropped: strict providers could not send it,
  and a coordinator following the prompt got a plain timed pause instead.

- Obligations have no owner-coverage check: one whose owners all wait or stall (a waiting hand-off,
  a resource backoff, a review or fix stack that stopped moving) is caught only by the 2 h
  durable-progress rule, or by a milestone falling due. A daemon-side check of whether some owner
  could advance it was dropped: it took ordinary waits, backoffs and review and fix stacks for stuck
  owners and raised high events that woke the coordinator.

## Liveness, progress and deploys

A project once stayed busy for days, finishing tasks with a fresh heartbeat, while its daemon ran
a release many versions old behind an upgrade task that could never finish, and its deploys still
counted as successes. Each claim below needs its own evidence and one owner.

- Liveness is not progress. A fresh heartbeat, busy workers and finished tasks say the daemon
  runs, not that the user's goal moves. Progress is a durable outcome: an obligation's 2 h
  durable-progress clock per owner, and a milestone with a due time that raises one
  `obligation_overdue` episode however busy the project is. The last completed tick is kept apart
  from the heartbeat, which a long tick's steps also touch for the watchdogs.
- Files are not running code. The daemon captures a runtime identity receipt once, at startup
  (`identity.py`): version, source commit, runtime path, pid, process start, boot id and instance
  id. Nothing re-reads files or infers it from git later. Status, overview and the web app show it;
  a running version that differs from the files reads "restart pending", no receipt "unverified".
- Deferred is not deployed. A deploy is accepted only by `ttp deployed [<version>]`: exit 0 once
  every project `ttp list` shows here reports that version and commit by its receipt and has
  completed a tick since, 1 while any is pending (one line each with owner and resume time), 2
  when it cannot tell. A deferred merge or a restart not yet done is pending, never success.
  `--after-push` records pending merges that have an owner and takeover time instead of paging.
- A project's own task cannot block a host-side upgrade. An explicit host-side `ttp upgrade` takes
  over the finish lock of an upgrade task blocked, or with no run or update, for 6 h
  (`release.STALE_S`). It fences that task by generation, so its later `--apply` is refused, and
  keeps the upgrade lock, push protection, ancestry checks and local edits. A finish task stuck
  that long sends one durable escalation to the maintainers' upstream inbox.
- One owner per gap. A check whose named target is missing becomes one gap keyed by its signature,
  with one repair task (`checkgaps.py`). Dependent tasks wait on it with a probe and rerun
  model-free once the check config changes; they are not blocked, and they spawn no fix and
  re-review pairs. A related change that lacks the test still fails. "No tests collected" is never a
  pass, and nothing is skipped wholesale.
- A cross-project hand-off is done only when the consumer acknowledged it and has recovered: its
  own daemon runs the fix by its receipt and the original condition rechecks clean. Filing a note
  or pushing the fix is not closure. Planned: tracking each note through resolve, release, wake,
  acknowledgement and recovery under one fingerprint, with a probe for "the fix runs here" that a
  task waiting on it can use. Until then a worker waits on an upstream fix with `ttp deployed` or a
  version probe as `retry_when`, never `blocked`. A note forwarded from a machine the maintainers
  cannot reach is closed over the same exchange that forwarded it: the sender pulls, the receiver
  never pushes. Each answer to the sender's forwarder carries back the resolutions of that sender's
  notes, and the sender's acks go out with its next send. This keeps one channel, one direction of
  ssh and one cursor per target. Pushing back would need a new route into a machine that may sit
  behind NAT.

Accepted gap: there is no fast-fail for a schema 400. When a provider rejects the coordinator
action schema (HTTP 400), the turn gets one ordinary retry, bounded by the coordinator turn timeout
sized to provider and effort (`coordlimits.turn_timeout`). Prevention is the strict-valid action
schema and its recursive strict-rules test, which catch a schema 400 before release. Three
fast-fail attempts were retired after failed reviews: their changes to the provider parse callers
and the live supervisor parse broke recovered-success handling and the timeout, budget and STOP
checks. Any future attempt must leave the provider parse callers and the runner's watch loop as
they are.

Precedents:
- Kubernetes Deployment `progressDeadlineSeconds`: a stalled rollout is reported apart from pod health.
- Prometheus `*_build_info`: a process exports the version and revision it runs.
- systemd `sd_notify`: the service itself reports readiness and status, not its unit file.
- Transactional outbox: a state change and its message commit together; consumers deduplicate.

## Why the coordinator is tool-less

A default headless Claude Code run carries tens of thousands of tokens of built-in context. A
decision turn with a replaced system prompt, no tools and a JSON schema costs a few cents. The
coordinator therefore reads a digest and delegates anything needing files or commands.
The stable part (role, charter, memory) is the system prompt, which providers cache.

## User corrections

When the user corrects a fact the project stated (a status, result, figure or owner), the
coordinator records it that turn as a `fact` starting `Correction (user msg #<id>):`, superseding
the entries that stated the old claim, and queues a task to fix any project file that states it.
For 30 days `Project.user_corrections` lists the newest 10 such entries (2000 characters at most,
oldest dropped first) under "User corrections (outrank project notes and older output)": first in
every worker's prompt, ahead of the rest of memory and outside its budget, and as a live section of
the coordinator's digest, where they are pinned past the memory budget and left out of the other
recent facts. After that a correction is an ordinary fact again. Workers check that list before
restating a status from a note, and fix their own project's notes that contradict it.

## Adding a provider

What each supported CLI offers, with doc links: [providers.md](providers.md).

1. `runtime/ttp/providers/<name>.py`: subclass `Provider`; `build`, `parse`, `account`,
   `login_hint`, optionally `meter` (only if it costs no model tokens), `cost_so_far` (if it
   streams usage but cannot enforce a budget itself), `writable_args` (if workers run in a write
   sandbox) and `plugin_args`. Price estimated tokens with `price_row(PRICES, self.prices,
   self.model)` so `pricing.<provider>` in project.json applies. Set `isolate_read_only` when the
   agent loads instructions from its working directory, so coordinator turns run from an empty
   scratch directory. Probe newer CLI flags with `cli_output(exe, "--help")` and keep the older
   command when they are missing.
2. Import it in `providers/__init__.py:_load_all`.
3. Add default tiers in `project.py:DEFAULT_CONFIG`.
4. Add a parsing test with a recorded output sample.

## Project harness lifecycle

- `ttp new` copies `runtime/`, `template/prompts`, `template/bin` into `<root>/tt-project/harness/`,
  commits them on branch `upstream`, then commits charter and config on `main`.
- Projects change their own harness on `main` (harness tasks, the daily review).
- `ttp upgrade <name>` commits the installed template on `upstream`, merges it into `main` in a
  scratch worktree, and fast-forwards the live harness only when the merge is clean and the
  runtime compiles and imports. Otherwise it queues a harness task and changes nothing.
  First it commits local changes as "local harness changes before template upgrade". While harness
  tasks run, it commits only files changed before the oldest such run started (not the upgrade's own
  run) and leaves newer files and deletions for their worker to commit with its own message. It does
  not wait for those runs, which could hold the upgrade for hours. If a left file blocks the
  fast-forward, the upgrade stops with nothing changed, and a rerun after the worker commits applies it.
  It holds the `harness-upgrade` lock (state/locks, released by the OS when it exits) for the whole
  merge, refuses while another upgrade runs, and does not commit when `main` changed runtime/,
  prompts/ or bin/ meanwhile. The open upgrade task holds the live harness until it ends
  (state/locks/harness-upgrade.task; stale once the task is done, failed, cancelled or gone): a
  later `ttp upgrade` commits the newer template on `upstream`, retargets that task (its spec and
  its running worker's steer.md) and exits 75, never merging into `main`. The task applies its resolution with
  `ttp upgrade <name> --apply <commit>` (fast-forward only). A reference-transaction hook in the
  harness repo refuses every other update of `main` that newly takes in `upstream`.
  Runtime files `main` deleted, emptied or cut short are restored from `upstream`. A file that only
  lacks top-level names `upstream` ships is kept (a deliberate local edit) unless the merged runtime
  fails to compile or import without them; the upgrade warns and records the names.
- Each daemon compares `~/.tt-project/lib/current` (version and source commit) with its own
  harness runtime at start and then hourly. While the installed release is newer (or the same
  version from another commit), `ttp status` and the web app show `tt-project <installed>
  available, harness on <current>`. With `upgrade.auto` on (the default) the daemon runs the
  installed `ttp upgrade <name> --auto` for its own project only, detached, when no push and no
  other upgrade is in flight. That restarts the daemon (workers are kept) and posts one low
  notify. Each release is tried once: conflicts that need no judgment (both sides only added at
  one spot, or upstream already ships the project's change) are settled without a model; any
  other conflict leaves one harness task and no hourly retries. At most one such task is queued
  a day; a conflict within a day of the last one waits and is tried again after it. `ttp config <name> upgrade.auto false` opts out; `ttp upgrade <name>` then applies it.
- Power-cycle notices (cyclenotice.py). A device broker on the same host announces a pending power
  cycle in a notice file, by default `/run/tt-device-broker/power-cycle-pending.json` (JSON with
  `state` "possible" or "requested", `reason`, `earliest_cycle_at` and `updated_at`). While one holds,
  `ttp upgrade` and `ttp setup` change nothing and exit 75 with one line naming the state, reason and
  earliest cycle time; the daemon starts no automatic upgrade or host-side takeover, and a post-push
  deploy (`delivery.after_push`) waits. Each goes ahead by itself once the file is gone, with no try
  spent and no failure counted. `upgrade.cycle_notices` (a list of paths; `[]` turns the hold off) sets
  the files; a missing file holds nothing. A malformed file holds for 10 min after its last write, a
  well-formed one for 6 h after its last update. `--ignore-cycle-notice` proceeds with a warning.
- Interrupted upgrades (upjournal.py). `ttp upgrade` journals each step in `state/upgrade.journal`
  before it changes the harness. A run cut short (a reboot, a kill) is recovered under the upgrade
  lock by the next `ttp upgrade` and by the daemon at start and on its release check: before the
  merge it is dropped; mid-merge it is rolled back to main (an unfinished merge aborted, touched files
  put back); after main moved it is finished, and a daemon left stopped or on the old code is
  restarted. An automatic upgrade cut short is retried, at most 3 times in a row. `ttp setup` journals
  its copy in `lib/.setup.journal` and switches `lib/current` in one rename; the next `ttp` command
  finishes a cut-short copy from the same plugin folder or, with that gone or now holding another
  version, rolls back to the release before. A post-push deploy a reboot cut short is run once more by the daemon.
  An explicit host-side `ttp upgrade <name>` (not `--auto`, not from one of the project's runs) queues
  the merge task once the last one ended `upgrade.merge_retry_h` (6) hours ago, and takes the finish
  lock over from a finish task that is blocked or has had no run or update for 6 h: that task is
  cancelled (superseded) and the lock's generation goes up, so its later `--apply` is refused. A
  finish task stuck that long is reported once to the maintainers' upstream inbox; a restart does not
  repeat it. Deferral lines name the owner and say when the upgrade resumes, in the home zone.
- How a release reaches `lib/current`: `ttp setup`, run from a plugin's root, copies that plugin
  into `~/.tt-project/lib/<version>` and points `lib/current` at it. It refuses to replace a
  newer installed version unless run with `--force`, which leaves a `lib/forced-downgrade` marker
  naming the version it installed. If `lib/current` still ends up older than a project's harness,
  that project's daemon points it back at the newest complete `lib/<version>` at the harness
  version or newer, and posts a low notice. It leaves a forced downgrade alone and raises one
  self-clearing alert when nothing newer is left to point at. With Claude Code, a plugin
  update (`claude plugin update tt-project@<marketplace>`, or the marketplace's auto-update)
  only puts the new version in Claude Code's plugin cache. The skill runs
  `<plugin-root>/bin/ttp setup` again when `ttp --version` is older than the plugin it ships
  with, so the first session that uses the updated plugin installs it, and every project's
  daemon notices it within an hour. Running that `setup` by hand does the same.
- The daemon holds `state/daemon.lock` (flock) while it lives and touches `state/heartbeat` after
  every completed tick. `status` and the web app report a stale heartbeat. `ttp restart` waits
  for a fresh one; without it, `runtime/` is restored to the last commit a daemon ran on.
- A git command killed mid-write leaves its `.git/*.lock` behind and every harness commit fails
  after it. Before each harness commit, and in the daemon's harness sweep, such a lock in the
  harness repository is moved aside to `.git/ttp-stale-locks/` once it is older than
  `harness_git.stale_lock_s` (600 s; 0: off; the old `watchdog.stale_git_lock_s` counts while the new
  key is unset) and no process holds it (none has it open, and no git process of the user runs in
  or names the repository), and logged (`ttp.gitlocks`).
- Before a task's workspace is made, run-start checks (`ttp.startcheck`) look for what a reboot
  or cleanup took away. A worktree whose git dir is gone moves aside to `worktrees/.stale-<time>/`.
  If its repository is gone too, the task is blocked, since its unpushed commits went with it.
  Otherwise the worktree is made again on its branch. A root that was a git repository and has no
  `.git` now blocks worktree tasks. A root or worktrees folder on tmpfs or ramfs is reported, and so
  is a missing or empty `~/.ssh/known_hosts` while the project uses ssh. Each finding is an
  observation (once a day) and goes into the worker's prompt, the run's note and the stored hand-off
  (`start_checks`).
- A failed run start leaves its task queued, no attempt spent, to retry in a minute. When one cause
  (the same exception type at the same runtime file:line) fails the starts of 3 different tasks
  within 10 minutes, the run-start breaker (`ttp.startbreaker`) opens: one `run-start` alert names
  that line, no worker starts, and one start retries every 3 minutes. It closes once a start
  succeeds, the retry fails elsewhere, or the config or runtime changes. While it is open, the
  coordinator's repeated-failures trigger skips runs that never started.
- Stopping or restarting the daemon leaves running workers alone; the next daemon adopts them.
  A cancel ends the task's runs; `ttp stop --kill` ends all runs and requeues their tasks.
- Generic lessons from a project come back as follow-ups titled `upstream: …`.
  The daemon files each one, deduplicated by a fingerprint of title and spec, in the user's
  inbox `~/.tt-project/upstream.jsonl`. A project with `upstream.ingest: true` (off by default)
  reads it, and the inboxes on its remote projects' machines over ssh at most hourly, keeping its
  own cursor; new notes become `upstream_note` events for its coordinator. Coordinators pass the
  notes on to the user only while no project reads the inbox.
- A note from another project can always be answered: its route (sender, machine, the machine it
  came through) is kept in the reader's database, and `ttp note --reply <fp>` sends the answer
  back that way. A sender's machine that cannot be reached from here (a laptop behind NAT) takes
  the replies held for it in the answer to its next `ttp upstream --receive` call. With no route
  the reply is refused, never dropped.

## Maintainer notes

These are for people changing tt-project itself, not for running a project.

- Testing a branch before release: run `plugins/tt-project/bin/ttp setup` from a checkout at that
  branch's tip. The installed `ttp setup` reinstalls its own version and warns about it.
- Coordinator cache tuning (see providers.md). An earlier seven-day comparison found a token-hit
  increase from about 57% to 68.5%, but cost per turn rose from $0.073–0.083 to $0.127 as prompts
  grew and effort changed. Its "cold" threshold was below 30%, distinct from the existing status
  miss threshold of 50%. A better hit rate alone therefore does not demonstrate savings. Evaluate
  this change using three days before and three days after deployment, selecting turns with a 5–60
  minute gap since the previous coordinator turn ended. The status API's
  `coordinator_cache["3d"].gap_5_60m` reports token hit and cost per turn for that cohort, with raw
  counters and known/missing counts. Compare the same provider, model and effort, report prompt
  sizes and sample counts, and retain the change only if both token hit and cost per turn improve.
  If the sample or telemetry is insufficient, report unknown and extend observation; do not infer a
  miss or savings. Revert the tuning if the comparison shows no improvement while retaining the
  telemetry needed to diagnose it.

## Prior art

The design borrows from agent project managers that already exist:
- a coordinator that never writes code, with isolated workers returning structured handoffs;
- one dispatcher with leases, per-task workspaces, and stall and retry rules;
- a local tracker as the durable state;
- hard budgets in the harness, not in prompts;
- an attention inbox for the user.

It differs from them by being local, multi-provider and plan-window aware.
