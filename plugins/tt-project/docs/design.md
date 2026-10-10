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
| Billed spend | `runtime/ttp/billing.py` | which spend was billed: per account and time, the one filter every dollar total uses |
| Screening | `runtime/ttp/screen.py`, `providers/jev.py` | dedupe → rules → Jev → wake the coordinator or not |
| Watchers, schedules | `runtime/ttp/watchers.py`, `schedule.py` | model-free probes that report changes only |
| Providers | `runtime/ttp/providers/` | build argv, parse output, report account and plan windows |
| Web app | `runtime/ttp/web.py`, `web/` | JSON API + static page, localhost + token |
| Services | `runtime/ttp/service.py`, `notifier.py` | systemd user / launchd / cron watchdog; desktop notifier |

State is one SQLite file per project (`state/project.db`). Everything a run produces is on disk
in `state/runs/<id>/`, so a daemon restart never loses a result.

## Invariants (keep them; tests guard most)

1. Models never poll. The daemon polls; a model runs only for a decision or a task.
2. At most one coordinator turn at a time; turns are debounced, batched and capped per hour.
   Routine events (a task done, its follow-ups and notes, normal observations) wait up to
   `coordinator.batch_s` for company, only while every worker slot is busy or runnable work is
   queued; user messages, failed, blocked or review hand-offs, high events and an idle slot with
   nothing to run wake it at once.
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
   A wake that hands back the same wait (`waiting_for`, `retry_when`, branch head) made no
   progress: later wakes run light only, and after `waiting.max_stale_wakes` (3) the task is
   blocked for the coordinator to split or re-plan, and not woken again. The block, a requeue or
   a new spec starts the count over: the next run is at the task's own wake tier.
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
13. A question to the user carries a blocking reason (access, funds, spend, review, merge,
    irreversible, restriction, human) and waits for the user; anything else is a judgment call
    the coordinator decides and records. Its recommendation is shown so the user can answer in
    one word, but no new question falls back to it or to anything else on a timer. Questions asked
    before this rule with a default still drain: after `coordinator.ask_timeout_h`, never at a
    cap or when the user has written since it was asked (the coordinator is asked to confirm
    instead); the user is told what was decided, at `high` severity or above.

## Known gaps (accepted)

- A wait on another project of this machine gets nothing extra from the daemon: no note to that
  project, no default end, no matching of project names in `waiting_for`. The worker tells the
  other project itself with `ttp note --to <project>`. A wait that never ends is caught like any
  other: a timer-only wait is blocked after `budget.max_waits` tries, a probe that can never pass
  raises one `probe_never_passes` event, and one still saying not yet past `waiting.max_hold_s`
  raises one `wait_stale` event. A daemon-side version was dropped: it moved the end on every
  re-wait, sent the note again each time and matched project names too loosely.

## Why the coordinator is tool-less

A default headless Claude Code run carries tens of thousands of tokens of built-in context. A
decision turn with a replaced system prompt, no tools and a JSON schema costs a few cents. The
coordinator therefore reads a digest and delegates anything needing files or commands.
The stable part (role, charter, memory) is the system prompt, which providers cache.

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
- Stopping or restarting the daemon leaves running workers alone; the next daemon adopts them.
  A cancel ends the task's runs; `ttp stop --kill` ends all runs and requeues their tasks.
- Generic lessons from a project come back as follow-ups titled `upstream: …`.
  The daemon files each one, deduplicated by a fingerprint of title and spec, in the user's
  inbox `~/.tt-project/upstream.jsonl`. A project with `upstream.ingest: true` (off by default)
  reads it, and the inboxes on its remote projects' machines over ssh at most hourly, keeping its
  own cursor; new notes become `upstream_note` events for its coordinator. Coordinators pass the
  notes on to the user only while no project reads the inbox.

## Prior art

The design borrows from agent project managers that already exist:
- a coordinator that never writes code, with isolated workers returning structured handoffs;
- one dispatcher with leases, per-task workspaces, and stall and retry rules;
- a local tracker as the durable state;
- hard budgets in the harness, not in prompts;
- an attention inbox for the user.

It differs from them by being local, multi-provider and plan-window aware.
