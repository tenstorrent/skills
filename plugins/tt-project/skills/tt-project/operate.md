# Operating a project

## Commands

| Command | Does |
|---|---|
| `ttp list` | projects known on this machine |
| `ttp overview [--json]` / `ttp list --status` | one line per project on this machine: daemon running, stopped or stale; harness version, flagged when behind the installed release; open asks; tasks waiting on the user; running workers; spend today vs its cap; then the global daily cap. Other projects are only read; remote ones show "remote, not checked". The web app's Projects tab shows the same |
| `ttp find <name>` | locate by registry, then by chat-log locators |
| `ttp adopt <name> --host H --dir D` | record where a project lives (an alias for this machine is stored as its own host name) |
| `ttp status [<name>] [--json]` | daemon, spend vs caps, coordinator health, why idle, running/blocked/waiting tasks, open questions, a newer tt-project release; without a name, the project of the current folder. `--json` (also the web API state) carries `blocker_sweep`: the sweep no coordinator turn has handled yet, `{sweep, queued_at, why, events, items: [{key, fp}]}` (`fp` null for an unwatched item), or null when none is open |
| `ttp task <name> list [--status queued,waiting,...] [--all \| --limit N] [--json]` / `add "<title>" --spec … [--start-when "<cmd>" [--why "<words>"]] [--start-after <2h or ISO time>] [--budget <usd>]` | inspect or queue work by hand; `list` shows the newest 50 unless given `--status` (filtered before any cap, so every match shows), `--all` or `--limit`, and says on stderr when it cut the list; `--budget` is its per-run dollar cap (default: `budget.task_default_usd` for its tier, which dispatch also gives any task made without one); a deferral given to `add` is set with the task, so it never starts before its probe passes (same checks as `task_add`, e.g. `landed:#<id>`; a grep of the push branch's log is refused) |
| `ttp memory <name> "<fact>" [--kind K] [--standing] [--title T] [--audience workers\|coordinator]` / `--forget <entry>... [--why W]` | add a durable fact / retire stale ones to memory/archive/ (several names: one commit; an unknown one is reported, the rest still retire). `--forget` takes every word after it, so to add and retire in one call put the text first: `ttp memory <name> "<new fact>" --forget <old>`. Prompts get restrictions, preferences, resources and standing entries whole, then the newest decisions and facts that fit (the coordinator's keep a reserved share however large pinned ones grow; each write prints the pinned totals and warns when they crowd it). `--standing`: a duty that recurs; retiring it needs `--why` (its end condition or the user's words ending it). `--title`: the entry's title; `--audience`: who gets it (also with `--supersedes`; `default` clears it) |
| `ttp memory <name> "<merged>" --kind K [--standing] --supersedes <entry>...` | add a merged entry and archive the entries it replaces, in one commit. Refused whole, nothing written, if a name is unknown or archived, a standing entry would become a non-standing one, a restriction, preference or resource another kind (a preference may become a restriction), or the quoted words (the user's verbatim words) of a restriction, preference or standing entry are missing from the new text. It keeps the earliest end of the entries it replaces unless `--permanent` |
| `ttp memory <name> --search <words>` | list live and archived entries holding every word (any case); read-only. Once a day the daemon archives the decisions and facts (never standing ones) that every prompt has left out for `memory.archive_hidden_days` days (default 7; 0 = off), with one commit and one low feed line; search still finds them. The digest flags pinned entries over half the coordinator's memory budget |
| `ttp skill [<name>]` | print the path of this release's SKILL.md for a tt-project skill (e.g. `tt-project-harness`), readable from any project and agent; no name lists them. `ttp setup` installs them under `~/.tt-project/lib/<version>/skills/` |
| `ttp config <name> <key> [value]` | read or set settings (dotted keys) |
| `ttp pause <name> [--reason ...]` / `resume` | stop starting model runs / start again. Every pause records who (user, coordinator, a run, another project, the daemon), why (required from a run) and since when; status, the web app and the digest show it. A pause longer than `budget.paused_alert_s` (default 4 h, 0 = never) raises one normal alert, reminded at most daily, cleared on resume |
| `ttp pause <name> --until-reboot --reason "host power cycle"` / `ttp drain <name> --wait <s>` | pause until this host reboots (the daemon lifts it on its first tick on a new boot) / pause and wait until no run or push is in flight (exit 0), or exit 1 at the timeout; running workers are kept. It lists the `ttp detach` jobs a reboot would end (`--jobs` waits for them too); `--reboot "<why>"` records a planned host cycle, so the jobs it cuts are not counted as reboot losses, and holds every project on this host (when it is in `ttp machines list`): no new worker, reviewer or push runs, `ttp checks` exits 75, and the wait covers their runs too, until the intent closes, the host boots again or 10 min past its planned time |
| `ttp machines add` (or `set`) `<alias> --tags device,... [--note ... [--until 3d]] [--min-free-gb N] [--hostname H] [--shared [names]]` / `ttp machines list` / `ttp machines remove <alias>` | the user's machines, shared by all their projects; each charter says which ones a project may use. A note's `--until` marks it stale in the coordinator's digest once passed. `--min-free-gb` sets the disk guard on that machine's filesystem for every project there (0 = off, "" = the projects' own); `--hostname` when the alias is not its short host name. `--shared` makes its resources (default: the alias) one set of lock slots and one pause across all projects (also: config `shared_resources`); where the projects' `resources` counts differ, all use the smallest. A paused shared resource cannot be unshared or removed until resumed; one dropped from `shared_resources` stays paused in that project |
| `ttp machines set <alias> --owner <project> [--fallback <project>]` | the machine's recovery owner: when a project using it sees it down or held past its grace (a command watcher's observation with `machine` and `condition`), the daemons page the owner model-free, the fallback when the owner cannot act for 30 min (daemon down, paused, logged out), then the user as an outage that clears once no project reports it (or the reporter's coordinator, when its heal check has `escalate: coordinator`). An outage whose owner cannot act, or records no change-ledger entry on the machine (`ttp machines change add`) within `heal.outage_owner_budget_min` (default 60) of its start, becomes a tracked task labeled `outage:<alias>:<condition>@<start>` in the fallback (else a detecting project), due `heal.outage_task_due_min` (default 30) after it is queued; still open past due (failed counts as open; done or cancelled close it), one high page to that project, then `heal.outage_escalate_min` (default 30) later one user alert. Open outage tasks show in `ttp status`, the web app and the blocker sweep; observation mutes never stop them. A page that did not go out is retried, never lost. With none set, each project using it is told once. Not escalated past the owner: a fault a heal check names in `known_fault`, a pause of a resource on the machine (by the user, a shared one, or the reporter's own), or no open task using it; a known outage or an open change that `--expect`s it pages nobody. Each such outage is listed once a day in the daily review instead |
| `ttp machines outage <alias> "<why>" --expires <t> [--owner <who>] [--covers <parts>]` / `ttp machines outage [alias] [--json]` / `ttp machines outage <alias> --clear [--by <project>]` | a known outage (a hardware repair, an external ticket, a person who has it): nobody is paged about the machine until it ends, and the daily review lists it; each project keeps its own entry per machine (setting it again or `--clear` changes only that project's), and the machine stays out while any entry is open; `--covers` (host, power, network, tray, chip, fabric, or a service's name) tells co-tenant watchers which conditions it explains, and keeps quiet only the heal checks whose name, resource or unit names a covered part, never because of their output (host, power or network: also the runs that cannot reach the box) |
| `ttp ttr [<name>] [<alias>] [--days N] [--json]` | how long each machine took to recover from problems: incidents, median, 90th percentile and longest time, the ones still open and the last one (home zone); incidents that started during the project's own `--test` machine changes are listed apart, outside those numbers; the daily review shows one line per machine |
| `ttp push-audit [<name>] [--days N] [--json]` | done tasks of the last days (default 2) that say they pushed a commit the remote does not have, with counts confirmed / missing / unknown; the daemon runs it hourly, raises one issue per gap and puts one line in the daily review. It also closes an open ask whose tasks (named `#<id>` in it, or held on it) all landed after it was sent, by their own push or through a review or follow-up of theirs that the push queue landed; the daily audit shows it as `resolved: its work landed`. An ask naming no task stays open. A done hand-off that says it pushed stays `pushing` until the push is found on the remote. In a code repo with no git remote, a detached `ttp push --own` is checked on the local branch it moved; summary lines and `pushed` entries there are not verified |
| `ttp machines change add <alias> "<what>" --undo "<cmd>" (--expires <t> \| --until-probe "<cmd>" \| --until-task <id>) [--expect <fingerprint\|down\|held>] [--test] [--since <t>] [--auto-undo] [--allow-met-probe]` / `ttp machines change list [alias]` / `ttp machines change close <id>` / `ttp machines change edit <id> [--what "<text>"] [--undo "<cmd>"] [--expires <t> \| --until-probe "<cmd>" \| --until-task <id>] [--test \| --no-test] [--auto-undo \| --no-auto-undo] [--allow-met-probe]` | the ledger of live changes to shared machines (a drop-in, a disabled recovery switch, a pause), next to machines.json: every project with the machine in its Resources sees the open entries in its digest. `edit` corrects an open entry in place (same id, author and creation time; the entry keeps who edited it and the old values; a new end drops its overdue pages); it refuses closed ids and edits that change nothing. `--until-task <id>` ends it once that open task of the project is done, failed or cancelled (a staged build tree a follow-up needs; a continuation re-extends it with `edit <id> --until-task <its id>`; the daily review lists entries whose task is over). Overdue entries (expired, the until-probe exited 0 on two runs in a row, or the until-task ended; the probe first runs 5 min after the entry, keeps running while it is open, and a later "not yet" exit clears its pass) page the project that made them, then the owner, then the fallback, then the user; nothing is undone automatically unless the entry has `--auto-undo`. `--auto-undo` (at `add`, or later with `edit`) fits an idempotent, quick undo that is safe to run unattended (remove a scratch dir, re-enable a timer): once the entry is due (expired, or its until-probe passed twice in a row), only the recording project's daemon runs the undo, model-free and once, from the project root with stdin from /dev/null, in its own process group, under a 300 s timeout that kills the whole group; the claim is recorded before the run. Exit 0 closes the entry (`closed_by: auto-undo`); 1, 75 or 255 retry 5 min later, at most 3 tries; any other exit, a timeout, the third "not yet" or a run a daemon restart interrupted (never rerun) pages as an overdue entry would, with the output's tail. It never runs while the machine is under `ttp machines outage`, a fleet hold, or a pause of a resource on it: it waits, unpaged, and runs once that ends. `list` and `--json` show each run (try, outcome, output tail). `add` and `edit` refuse `--auto-undo` for a no-op undo, an undo `ttp killscan` flags (kills by name or pattern), an until-probe that always exits 0 or an `--until-task` end. An entry whose undo is a no-op (empty, `true`, `:`, or `none` alone or followed by `:`, `;`, `-` or a space, as in `--undo "none: nothing left to undo"`) is never paged: it closes itself, model-free and once, when its until-probe passed twice in a row or, with no until-probe or until-task, when it expires (never on a probe that still fails, broke or always exits 0); `list --all` and `--json` show `closed_by: auto-close: <rule>`. Use `none: <why>` only when nothing remains to undo, and never queue a task just to close such an entry. `add` and `edit` refuse an until-probe that always exits 0 (only `true`, `:` or `exit 0`, or ending in `|| true`, `; exit 0` and the like): end the test with `|| exit 1`; an older entry with one is never ended by it, only by its time, and is reported as broken. An until-probe must still fail when the change is recorded: `add`, and `edit` given a new `--until-probe` (left unchanged when refused), run it once (as the daemon would, at most 10 s) and refuse one that exits 0 (the entry would be overdue at once) unless `--allow-met-probe`, which stores it with a warning; a probe that times out, cannot run or exits with a broken code is stored with a warning. `--expect` names an alert the change causes, which keeps the coordinator's turn routine while it is open. `--test` marks a change made for a test (listings show `a test`): `ttp ttr` labels this project's incidents on that machine that start while it is open as tests, outside its time-to-recover numbers. `--since <t>` backdates the start of a change already made (a delay back or an ISO time in the home zone; not ahead, at most 7 days back). An `--expires <t>` or `--power-cycle-at <t>` (and `machines outage --expires`) is a delay or an ISO time: a zone-less time is read in the project's home zone (no project: the account's `budget.timezone`, else this machine's zone), one with an offset or `Z` keeps it; listings show the end in the home zone with its abbreviation. `edit <id> --test` (or `--no-test`) flags an entry, open or closed, after the fact, so incidents already inside its window are relabelled; only the project that recorded it or the machine's recovery owner may. `ttp machines change add <alias> "<why>" --power-cycle-at <t>` records a planned power cycle instead (no undo; expects `down`; closes itself an hour after T): about 10 min before T every daemon tells its running workers on that machine to commit and hand off |
| `ttp obligation add "<what>" [--owner <task>]...` / `attach <id> <task>...` / `progress <id> "<evidence>"` / `receipt <id> "<evidence>" [--milestone <key>]` / `drop <id> "<why>"` / `list` | an urgent deliverable owed to the user, tracked on its owner tasks until a receipt exists. Each owner has its own 2 h durable-progress clock that only its recorded progress resets (not edits, tokens, running or waiting); one high observation per stall goes to the coordinator, and one per milestone past its due time without its `--milestone` receipt. Nothing checks that an owner can advance it: owners that all wait or stall are caught only by the 2 h rule. Inside a run, or in its task's `on_pass_cmd`, an owner records `progress` and `receipt` |
| `ttp pause <name> --resource <r> [--reason ...] --until <2d or ISO time> \| --end-when <probe> [--report-from <project>]` / `ttp resume <name> --resource <r>` | hold every task that uses resource `<r>` and make `ttp lock <r>` refuse it; running workers on it are told / lift it. An end (at most 7 days, or a read-only probe) is required; when it passes the coordinator lifts or extends the pause, except one the user set, which stays until the user lifts it. A probe that always exits 0 (`true`, or ending in `|| true`, `; exit 0` and the like) is refused: end the test with `|| exit 1`; an older pause with one is never lifted by it, only by its time, and is reported as broken |
| `ttp pause <name> --machine <alias> [--reason ...] --until <2d or ISO time> \| --end-when <probe>` / `ttp resume <name> --machine <alias>` | one machine-wide hold: every resource on `<alias>` waits in all of your projects on this host, and `ttp lock` refuses any name on it; status, the web app and `ttp machines list` show it once. A hold you set from the shell is yours: only you lift it (from the shell, or in a message to any project's coordinator). The setter's daemon checks its end; when it cannot, the machine's recovery owner does |
| `ttp lock <r> -- <cmd>` / `ttp lock --probe <r>` / `ttp detach <job> -- <cmd>` | run one command holding resource `<r>` (waiters in arrival order; 75 when busy or paused; `--shared` lets read-only commands hold it together, never on a device lock; shared and nested rules: `ttp lock --help`) / exit 0 if `<r>` is free now, 75 if not (`--json`: the live holders with pid and liveness; a keeper of detached jobs is named as the holder; a pid from another PID namespace, such as a sandboxed taker's, is marked so and judged through that namespace; from a sandbox that cannot see it, through its run's folder (a keeper's hold.beat, the run's lease); liveness unknown, naming the route tried, only when neither answers) / start a job that outlives the worker's run (from a sandboxed command the daemon starts it; a job gone without an exit code says why); the task then waits on `ttp detach --check` (0 ended ok, 4 a job failed or is gone, 1 running), and an `exclusive:` task's resources stay held until the job ends (`--remote <ssh alias>` starts it on another host; its probe is `ttp detach --check --host <alias> <dir>/<name>`; `--rows <file> --after <n>` also wakes on a new result row, and on the end or a dead driver) |
| `ttp ci --branch <b>` / `--run <id>` | `retry_when` for GitHub CI: exit 0 once the commit's runs completed or a job hung (past 3x its recent median, floor 10 min, 90 min with no history), 1 while they run, 75 if gh cannot answer; prints `done:`/`hung:`/`running:` per run |
| `ttp upgrade <name>` | merge the installed tt-project release into the harness and restart; the daemon does this by itself unless `upgrade.auto` is false |
| `ttp deployed [<version>] [--commit <sha>] [--projects a,b] [--fresh-s N] [--json]` | accept a deploy: exit 0 only when every project `ttp list` shows on this machine (read at check time, so one registered mid-deploy counts) has a daemon whose own runtime receipt names the version and commit (default: the installed release) and that completed a tick since the files were installed and within N s (300). 1 while any is pending, one line each with reason, owner and resume time in the home zone (files behind: merge deferred or held by a finish task; files current but the daemon still runs old code: restart pending; a stale tick). 2 when it cannot tell (unreadable state, a heartbeat without a receipt, unverified provenance). An upgrade exiting 0 or 75 is not a deploy: a deferred merge is pending, never accepted. Read only and quick, so use it as the deploy's `retry_when` |
| `ttp goal suite_green\|artifact_fresh\|service_up ...` | stock model-free goal checks for a `retry_when`, `start_when`, `on_pass_cmd`, heal `check` or watcher: a test command exits 0 at the current head (`--min-tests N`); a file or glob's newest match exists, is not empty and is newer than `--max-age-h`, `--newer-than-commit` or `--newer-than`; a systemd unit is active and/or a URL answers an expected status. One watcher JSON line per observation; exit 0 met, 1 not, 75 could not check. See the tt-project-harness skill |
| `ttp restart <name>` | restart the daemon and confirm it runs; a runtime it cannot start with is rolled back; from a sandbox it asks the running daemon to restart (exit 76 while deferred, 1 on failure) |
| `ttp stop <name> [--kill]` | remove the service; keeps all data. Running workers finish unless `--kill` (their tasks resume on start) |
| `ttp task <name> cancel <id>` | cancel a task and end its running worker |
| `ttp task <name> requeue <id>` | queue a task that is not running again (blocked, waiting, failed): clears its blocked reason, its waiting state and its failed workspace tries, like Requeue in the web app |
| `ttp task <name> set-when <id> ["<cmd>"] [--start-after <2h\|time\|now>] [--on-pass-cmd "<cmd>"]` | re-point the probe of a task that has not started: a waiting task's `retry_when`, else its `start_when`; `""` clears it. `--start-after` changes its start_after (`now` or `""` clears it; `requeue` keeps it). `--on-pass-cmd` sets (`""` clears) a waiting task's model-free completion command, with or without a new probe; the run's own result.json still counts as the hand-off it waits on. Refuses running and finished tasks: to defer a new task, give `--start-when` to `add` instead |
| `ttp task <name> superseded <id> <sha>` | record that done task <id>'s work landed on the push branch as commit <sha> (re-landed after a push conflict), so the local-only check and `landed:#<id>` count it. Refused unless the task is done and <sha> is on the push branch, newer than the branch's base and changes a file the branch changes; covers the branch head at the time. A task that never had a branch needs `--ref <ref>` and only that <sha> on it. Also accepts a code task in review on the same checks, and a harness task (done or in review) whose <sha> is merged into the harness with nothing unmerged on its worktree branch; a task in review is then closed as done |
| `ttp task <name> rejudge <id> [--dry-run] [--why "<text>"]` | rerun the daemon's hand-off checks (the requested-cells check and the project's `worker.handoff_checks`) on done or failed task <id>'s stored hand-off at the current harness version, after a check that wrongly marked it `partial:` or `cells unreported:` was fixed. A changed verdict rewrites the summary's mark and the daemon's keys, prints the before and after and logs them as a `task_rejudged` event; a `continue:` hop queued only for a cleared mark is cancelled if it never started, else kept and reported. A mark another rule added (no daemon seal) is told from the worker's words by the run folder's result.json and cleared the same way. A failing project check, or cells still missing, never clears the mark; the status never changes, and a second run changes nothing. `--dry-run` shows the change without writing |
| `ttp task <name> abandon <id> "<why>"` | record that done code task <id>'s work will never land (its approach was dropped), with the reason and time; its branch is kept. The local-only check and its status line, the push audit and the landed-wait checks leave it out, and a `landed:#<id>` wait on it is raised as a broken probe naming the reason, so its waiter is re-pointed. Refused for a task that is not done, another kind, one whose work landed, or one whose work can still land (an open review, fix or continuation of it, or a push queue entry waiting to push it: cancel that first); a later recorded landing (`superseded`) clears it |
| `ttp task <name> show <id> [--json]` | print a task's title, kind, status, spec and stored hand-off (summary, question, follow-ups, notes, review findings), and what the blocker sweep last decided about it; read-only; works for any local project by name; an unknown id exits non-zero. `--json` adds `blocker_sweep`: `key` (`task:<id>`), `outcomes` (the latest recorded outcome, kept 30 days; `[]` when none) with every field always present and null when unset: `outcome`, `ts`, `sweep` (the sweep's number), `until`, `probe`, `next` (the next check), `why`, `item` (the task as that sweep saw it), `fp` (a watched hold's fingerprint), `ask`, `ask_pause`, `pause` (`{resource: fingerprint}` of a user pause); `none_why` (why `outcomes` is empty, else null); `current` (`{sweep, queued_at, fp}` of the open sweep covering it, else null) |
| `ttp note --reply <fp> "<text>"` | answer note `<fp>` (its `[note <fp>]`) from another project over the route it came by, even when that project is not in `ttp list`: through this machine's inbox, a forward target, or, for a machine that pushes notes here but cannot be reached from here, held in the inbox until that machine's daemon takes it on its next call (it asks at least hourly for a week after sending notes here). `ttp note --to <project>@<machine>` does the same by name. With no route it exits non-zero saying it was not sent; `--to` an unknown name that sent this project a note names the `--reply` to use |
| `ttp note --status <id>` | where a `ttp note --to` note stands (`<id>` as it printed): queued, filed-local, forwarded (in the inbox of the machine that runs the project) or delivered (that project's daemon read it); exits 0 once delivered, 1 while on its way, 2 unknown, so it serves as a `retry_when` |
| `ttp ack-wait probe\|settle --from <project> --hash <h>` | wait for another project's reply to a `ttp note --to` with no model run: `probe` (a `retry_when`) exits 0 once that project filed a note for this one naming `<h>` (the note id or sha256 prefix `ttp note --to` printed), 1 while not, 2 if it cannot tell; `settle` (the `on_pass_cmd`) writes a `done` (or `--status needs_review`) hand-off quoting the reply, as data, to `$TTP_RESULT` |
| `ttp broker-released [file ...]` | probe (a `retry_when` or `on_pass_cmd` check) for a device broker's hold ending: reads /health JSON and log text from the files or stdin (e.g. `ssh <box> 'curl -s <broker>/health; echo; tail -n 500 <log>' \| ttp broker-released`); exit 0 once /health shows `held` false with a healthy fsm, or, without a live answer, the log's last hold line is `hold released` (`STARTUP hold released ...`) or a `device_released` event; 1 while not. /health `held` true wins over an older release line |
| `ttp land <head> [--task <id>] [--root <repo>] [--dry-run]` | land a reviewed head on a push branch that is a local branch of a code repo with no git remote: replays its commits onto the branch's tip in a scratch worktree, runs the push checks (`delivery.push_checks`) on the replayed head, then fast-forwards the branch and records the landing (`landed:#<id>`; a review's verdict covers the new commit). Exit 0 landed, 3 conflict, 4 a check failed, 5 the branch kept moving, 6 its checkout is dirty, 75 busy; only 0 moves it. `--root` works in that repo instead of the current directory's (try it on a temporary clone with its remote removed); a landing in a repo other than the project's is not recorded, and there a main/master push branch needs no `delivery.allow_protected_push_branch` (no remote, nothing published), while the project's own main/master still does and the refusal names it; `--dry-run` writes nothing and moves no ref: it reports the replay, a conflict (exit 3), the checks it would run (it does not run them) and the records it would write |
| `ttp landed <sha>` / `ttp landed --task <id>` | probe: exit 0 once the commit, or the task's landing, is on the push branch, also after a rebase gave it a new sha (same patch-id, or same author, date and subject); 1 not yet. `start_when` `landed:#<id>` runs it |
| `ttp doctor <name>` / `ttp doctor --scheduler` | providers, accounts, Jev, notifications, web / the installed runtime's command-watcher lifecycle (reports at exit 1, an `"error": true` line repaired and reopening, a clean run closing all) in a scratch project, each tick in a fresh process: no model, $0, exit 1 on a failed step; run it after an upgrade instead of the repository's tests, which are not installed |
| `ttp doctor --loop <provider>` | one light code task end to end on a real agent CLI (dispatch, hand-off, review, push queue, landed) in a scratch project whose only remote is a local bare repository, ticking one daemon of its own: one line per step with the spend, capped at $1 and 15 minutes, exit 1 on a failed step; a missing or logged-out CLI exits 0 |
| `ttp alerts <name> --after N` | alerts since a message id |
| `ttp alerts <name> --ack <id> --why "<reason>"` | acknowledge the keyed alert episode of a message id shown by `ttp alerts`: closes it if its condition is over, else moves it to the feed, quiet until it clears; a live outage (logout, network, disk, machine, whole-box heal) is refused; keeps history, retries leave the original receipt unchanged, unknown ids are refused |
| `ttp issues list [--all]` / `ttp issues close <id-or-key> --why "<reason>"` | the project's open daemon and watcher issues (`--all`: closed too) / close a stale one nothing closes by itself (such as a `daemon:schedule-gap` after a host move) without a DB write: the reason and time go in its history and the activity feed; unknown ids and an empty reason are refused; a condition still live reopens at its next detection (`--project NAME` outside the project) |
| `ttp mute list <name>` / `ttp mute end <name> <source> <match>` | the project's observation mutes / end one now without a coordinator turn; its end summary still reaches the coordinator, naming who ended it |
| `ttp stats [<name>] [--days N] [--since T] [--until T] [--split-at T] [--tools] [--json]` | context re-read (cache-read) tokens per run (mean and median) and per $, model calls per run and first-call context, by role, task kind, tier and effort, and the runs that re-read most; runs with no model call (failed sign-in) are counted apart, outside the averages; `--split-at` compares before and after a change; `--tools`: the runs' tool output by category, with the share that is file reads |
| `ttp throughput [<name>] [--since 24h] [--apply] [--json]` / `ttp throughput --undo <id>` | where tasks waited over the window, per lane (worker slots, resource and device slots, checks slots, review sweep and stall, push batch, `landed:#` chains, deferrals, provider holds): count, median and p90 wait, the limit and its setting, peak use, and whether waits over the threshold (minutes for priority 1, tens of minutes otherwise) met the limit or idle capacity. `--apply` raises only the sizing limits (`budget.max_parallel_workers`, `review.sweep_after_s`, `review.unreviewed_per_pass`, `delivery.push_batch_s`; never `coordinator.review_stall_s`: a sooner stall alarm starts no review sooner) of a lane that was at its limit while work waited and has headroom (no spend gate, pause or busy CPU), by a step sized from the load; never a spend cap, budget, gate, lock, hold, pause or restriction. Each change is recorded with an undo (`--undo <id>`), shown by `ttp status` and the daily review. A user's speed complaint runs it model-free in the same tick and queues one priority 1 task listing the slow lanes it could not change (at most one open: lanes found later join it; a lane a closed one listed is reopened only when its waits or p90 grew by half or after a day) |
| `ttp audit [<name>] [--hours N] [--json]` | the daily review's self-efficiency audit: every ask with its blocking reason and answer, failed, lost, retried and continued runs, idle wakes, $ per done task by kind and tier, review-loop spend per change, reviews by diff size, tier and re-review, stuck work and overrides held past their end |
| `ttp web <name> --tunnel --keep` / `--unkeep` | keep the web app's tunnel up as a user service / remove it (remote projects; local view forwards are asked once per user, `ttp consent tunnel`) |

## What needs the user

- The top of `ttp status` and of the web app shows only open questions and problems active now.
  Everything else (FYI notes, decisions, reboots, cleared alerts) is in the feed below, newest first.
- Alerts clear themselves and keep their history: logged out → a login check passes (until then no
  run starts on that provider; the daemon asks its CLI's status, no model call); budget red →
  the gate leaves red; coordinator failures → a successful turn; disk low → space is back. Chats
  hear once that it cleared. A host reboot is information only.
- A heal alert also clears when its check moves to `escalate: coordinator` (including the project
  default), or `known_fault` covers its current failure. This closes the user alert while retaining
  the failing check; a prolonged whole-box outage still escalates. Acknowledge a keyed alert with
  `ttp alerts <name> --ack <id> --why "<reason>"` when the user already knows: a live one moves to the feed and stays quiet until it clears.
- A logout is one alert per machine and provider, however many projects wait on it, reminded after
  1 h, 4 h and 12 h, then daily, with the waiting projects, queued tasks and high events.
- While the coordinator cannot run (logged out, paused, budget red, repeated failures) for 30 min,
  the daemon sends new high conditions that persisted 2 h to the chat itself: one line each, at
  most one message an hour.
- The budget is a few plain lines: per plan window the percent used, time to reset and history
  (daily peaks for the 5-hour window, the last two weekly finals), and one line for dollar caps.
  Pacing, gate reasons and top spenders are in the web app's Budget tab.

## Security notes

- Only the user posts as the user. `ttp say` refuses inside a run: when the run's environment
  is set, and when a running run's agent is among the command's parent processes (so unsetting
  the environment does not get past it). Workers are denied the web app's token
  (`state/web.token`): a Read deny rule for Claude Code, and the Bash hook refuses commands
  that name it (Claude Code, and Codex when its hook runs). Codex has no read deny rules and its
  sandbox does not fence reads. Cursor gets neither: its CLI takes no deny rules or hooks per
  run (no flag or environment variable), only from a config file (`.cursor/cli.json` in the
  worktree or the user's own global one), which the harness does not write. A user who runs
  Cursor workers can add a `Read(...)` deny for the token to their own global Cursor config.
- These are checks, not a fence. Workers run as the same OS user as the daemon and the user's own
  commands, so a worker that tries hard (a process that leaves its agent's process tree, a script
  that reads the token another way) can still read what that user can. For a hard boundary, run
  workers under a separate OS account or in a sandbox that cannot read the project's `state/`.

## Budget

- Plan windows (subscription): the project stops at 90% of any window.
- Usage-billed: default caps $100 per 24 h and $200 per 7 days, per project (all providers together).
- A run cut off before it reports its cost counts at an estimate, labelled as such.
- Tier routing (off by default): `providers.chat.tiers.<tier>.route: true` sends that tier's unpinned
  worker runs to the OpenAI-compatible `chat` provider; endpoint faults fall back to the core provider
  with no attempt spent and a cool-down (`route_cooldown_s`, default 900). A routed run that times out,
  fails or ends without a valid hand-off also falls back, once per task, recorded with its cause. Per
  tier: `effort` with `effort_fields` sets the reasoning effort sent; `route_timeout_s` the time limit. Spend uses `pricing.chat`
  (unpriced: $0, labelled). `ttp status` shows a `routed 24h:` phrase; `ttp stats --runs --tier <t>` lists runs.
- Gates tighten as spend rises: `green` → `yellow` → `orange` → `red` (paused).
- A spend spike far above the project's norm pauses it ("runaway guard").
- Raising a cap: tell the coordinator, or `ttp config <name> budget.daily_usd <n>`.
  Warn the user to raise caps carefully.

## Health

- Daemon not running → its service restarts it. Hung (alive, but its heartbeat untouched for
  `watchdog.hang_s`, 900 s by default, never below 600) → `ttp.watchdog`, run every 5 min (a systemd
  user timer, a launchd agent or the crontab entry), sends it SIGUSR1, saves its thread stacks under
  `state/incidents/` (the daemon writes every stack dump to `state/daemon-stacks.log`: on SIGUSR1,
  on SIGUSR2 for a dump by hand, and when a tick overruns or stalls; past 1 MiB it moves to `.1` at
  daemon start), restarts it through its service (`systemctl --user restart` with a 60 s timeout,
  then stop, kill and start) and sends one `daemon-hang` alert naming the dump and the restart's
  outcome; it clears once a daemon ticks again. Each restart within `watchdog.window_s` (6 h) doubles
  the next wait; the `watchdog.restarts`-th (3) sends one high alert. systemd's `WatchdogSec` is only
  the backstop. Only a service installed before the watchdog needs `ttp restart <name>` (which adds
  it); still down → `ttp logs <name>`.
- Linger (Linux, systemd service only): the daemon checks `loginctl show-user` at start and daily.
  While linger is off, one normal `linger-off` alert says the daemon and its watchdog start only at
  the next login after a reboot and names `loginctl enable-linger`; it clears once linger is on.
- Downtime: a daemon that starts more than `daemon.down_notice_s` (3600 s) after its last heartbeat
  posts one message: how long it was down, why (reboot or stopped), schedule runs missed and runs
  cut short, merged into the reboot notice when there is one. A short restart posts nothing.
- Only one daemon runs per project (a lock file), whatever starts it.
- Disk guard: free space under the project folder below the smaller of `disk.min_free_pct` (5) of
  the disk and `disk.min_free_gb` (150) → only question and plan tasks start; running work goes on;
  one high alert per episode, cleared once free space is back at `disk.resume_free_gb` (unset, or
  at least the disk's size: 1.2× the threshold; below the threshold: the threshold; a value below
  `disk.min_free_gb` makes `ttp doctor` warn), so a disk hovering at the line does not flap; the episode survives a daemon restart. Status, the web app and
  the coordinator's digest show free space. A machine whose entry in the machines list has
  `min_free_gb` uses that instead of `disk.min_free_gb` (for a shared disk that other services keep
  near full by design) and resumes at 1.2× it. The alert, and the digest while low, say how much of
  the used space is this project's own data (its `state/` and `worktrees/` folders, measured per
  subfolder; the biggest consumers named, e.g. "run worktrees: 2, 9.5 GB"; a measurement cut short
  gives the partial total it reached and says so) and name the biggest top-level directories (`du -x -d 1`,
  stopped after 30 s and killed; never from / or a home folder, nor over a network or FUSE mount:
  those show as unknown), so a full shared disk is not taken for project growth.
- When a task ends (done, failed, cancelled), its worktree loses its git-ignored build and cache
  directories (`disk.cache_dirs`) and is removed once clean with HEAD on a branch. Branches are never
  deleted, so `continues` still starts from the old commits. One whose only dirty entries are untracked
  files (HEAD on a branch) has them moved to its last run's `worktree-leftovers/` (relative paths kept)
  and is removed, if they total at most `disk.worktree_leftovers_max_mb` (50; 0 never). Hand-off
  artifacts (any task's `result.json` `artifacts`, globs too; ignored or untracked), small ignored
  `*.log` files (up to 1 MB each) and larger untracked files are archived first under
  `state/archive/tasks/<id>/`: a `files.tar.gz` (a new name per archiving; a file over 256 MB is stored as it is under
  `objects/<sha256[:2]>/<sha256>`, so identical content is kept once and archived files are never
  overwritten)
  and `manifest.json` (path, size, sha256, stored where; the task's status, branch and head).
  Unnamed untracked files are archived smallest first up to `disk.worktree_archive_max_mb` (200) per
  task; the rest are only listed there and deleted. `state/archive/tasks/MANIFEST.md` gets one line
  per archive. `ttp prune <name> --archived <id>` lists one (with `--restore <dir>`: extracts it).
  Archiving is all or nothing: one that would not leave the disk guard's threshold free on `state/`'s
  filesystem (1 GB once the disk is already below it) archives nothing and keeps the worktree, saying
  the sizes; a failure part way removes what it wrote and keeps the worktree as it was. Removals are
  done at once, but the daemon archives at most one worktree per tick step (the rest on the next
  ticks; a skipped or failed archive does not use up the step, and a failed one is retried only once
  its task changes or after the keep recheck, even under the disk guard); `ttp prune` archives them
  all. Every new entry is checked against what was stored (sha256) before the worktree loses anything.
  Known gaps: a kill mid-archive is finished or rolled back by the next sweep through the manifest's
  pending entries (the files stay in the worktree until then); archives written before the `objects/`
  layout keep big files under `files/<path>`, still readable, but one of two copies of the same path
  may already have been overwritten there.
  One with modified tracked files is kept and raised to the coordinator once per content (event
  `worktree_uncommitted`); `ttp status` counts them and the web app lists them in the feed. Under the
  disk guard, and with `ttp prune --patch-tracked`, a done or cancelled task's tracked changes are
  saved instead as `tracked.patch` (checked to apply to its head; `git apply` restores them) and the
  worktree goes. Any other dirty worktree is kept and listed, and so is one with submodules set up
  (their commits may exist only there). A stale "initializing" lock (an unfinished `git worktree
  add`) is cleared; any other lock, or a process running in the worktree, keeps it. A worktree is
  left untouched for at least an hour after its task ends (not once a review of it has ended, pass or
  fail), until the coordinator has seen the result,
  and while an unfinished task still needs it (it depends on or continues the task, or its spec names
  the task's branch, id or `worktrees/tN` path). `disk.worktree_retention_days` waits longer; 0 never tidies.
  When the disk guard holds work, the daemon sweeps first (at most every 15 min), and releases the
  held work if free space is back above the guard; the alert says what the sweep freed.
  Git worktrees a worker made inside its run directory (`state/runs/<id>/<name>`, found with `git
  worktree list` of the project's repo and of each such folder's own repo) are swept the same way
  once their run has ended: kept while the run is live, its task is open, it ended under the grace
  above, it has hand-off artifacts not yet archived, uncommitted changes, or a lock; then `git
  worktree remove` and `git worktree prune`. Stray worktrees workers made elsewhere under the
  project root (git's records of its repo and of each repo directly in the root, in a folder named
  `t<id>…`, `r<id>…` or `wt-<id>…`, e.g. `state/worktrees/t12`, `worktrees/t12-review`,
  `<repo>/.wt/t12`) go once task <id> is done or cancelled, nothing holds it as above, and its HEAD
  is on a remote branch; uncommitted files up to 5 MB are first copied to
  `state/archive/worktrees/<folder>/` (with a patch of tracked changes), more keeps it. An open
  task's are never touched, nor anything in another project's folder. Other worktrees stay.
  `ttp prune <name>` sweeps now (`--dry-run` lists what it would do) and reports worktrees removed,
  GB freed and why each other one was kept. Each keep reason is logged once.
- Workers and reviewers run with the project's Python venv active (`VIRTUAL_ENV`, `PATH`; their
  prompt names it), so a fresh worktree does not rebuild one. `worktree.venv`: `auto` (default) finds
  `.venv` or `venv` in the project root, a path names another, `""` turns it off. A worktree with a
  venv of its own keeps it; with no venv, runs start as before.
- Every worktree of the project's repository (a code or fix task's, one a reviewer works in or made
  itself, where `ttp checks` or `ttp push` runs, and the push queue's checkouts) gets a symlink to
  each `worktree.link_paths` entry (default `[".venv"]`,
  relative to the project root) that exists and is git-ignored in the project checkout and is
  missing in the worktree, so a check such as `.venv/bin/python -m pytest` works there. Tracked
  paths and existing files are never touched; the link is git-ignored (info/exclude if needed), and
  removing the worktree deletes only the link. `[]` turns it off. Check commands can also use
  `"$VIRTUAL_ENV"/bin/python`: workers and reviewers run with the project venv active.
- Harness commits (charter, memory, schedules) heal themselves. Each git command gets `harness_git.timeout_s`
  (default 120 s); every failure goes to `daemon.log` with git's exit code and stderr. Lock files in the
  harness's `.git` older than `harness_git.stale_lock_s` (600 s; 0: off) that no process holds (none
  has it open, no git runs there) are moved to `.git/ttp-stale-locks/`, never deleted. The next successful commit, and the daemon every
  `harness_git.sweep_every_s` (600 s), commit charter and memory files a failed commit left behind. Files that look like a
  power-cut write are left for the boot integrity check, and nothing is swept before it has run.
- Making a task's worktree recovers by itself. Each git command gets `worktree.git_timeout_s`
  (default 120 s), scaled by the host's load per CPU up to `worktree.git_timeout_max_s` (900 s). After
  a timeout or a failure left behind by one, a whole worktree is reused and a half-made one (still
  locked "initializing", with no index or HEAD) is removed and made again (`worktree.create_attempts`,
  default 2). A check that fails is left as it is and counts as a failure: an error never deletes a
  worktree. A branch with its own commits is never reset. A worktree on another branch than the
  task's is recovered: a branch with commits of this task (or the task it continues) is adopted; one
  with no commits of its own has its untracked, ignored and modified files (and `uncommitted.patch`)
  moved to `state/worktree-leftovers/t<id>-<date>/` and is made again on the task's branch. Ignored
  build and tool caches (`disk.cache_dirs`) are not moved: they go with the old worktree, except
  hand-off artifacts inside them, which move. Leftovers
  folders are deleted after `disk.worktree_leftovers_keep_days` (30; 0 keeps them). Any other case
  (another task's commits, submodules, the branch checked out elsewhere) blocks at once. If that still fails, the task is queued again `worktree.create_retries` times (default 3)
  after `worktree.retry_backoff_s` (120 s, doubling). Only then is it blocked (`workspace:`) for the
  coordinator. Tasks an older daemon blocked this way are queued again on their own. Checkouts
  leave Git LFS files as pointers (`GIT_LFS_SKIP_SMUDGE=1`), so a large download never outlasts the
  timeout; a task that needs them runs `git lfs pull`, or `worktree.lfs_smudge: true` downloads them.
- Work only on this machine: when a code task hands off done, and hourly for code tasks done in the
  last 14 days, the daemon fetches (30 s timeout) and checks whether the task branch's work is on a
  remote: a remote-tracking branch contains its head, or its changes are already on the delivery
  branch or the remote's default branch (rebased, cherry-picked, amended or batched by a reviewer:
  the same patch-id, the same author, date and subject, never a subject alone, or the whole change
  as one commit), or a task recorded the commit that carries it after a re-land with
  `ttp task <project> superseded <id> <sha>` (refused unless that commit is on the push branch, newer than the branch's base and touches its files;
  work committed on the branch after the record is flagged again).
  Tasks an unfinished task still needs (a queued review or fix), tasks a done review names, and work
  done before the check first ran are left alone. Otherwise one coordinator event says so, and
  status and the web app count it until the work reaches a remote, the task is cancelled or it is
  14 days old. Nothing is pushed automatically unless `delivery.backup_remote` names a git remote:
  then each done code task's branch is pushed there, fast-forward only (never forced, never to main,
  the push branch or the base_ref). A repository without a remote is skipped. A hand-off also
  notes, once per set of paths, uncommitted changes to tracked files in the main checkout.
- Project root left changed by a run → one low alert (`root-checkout:<task>`, plus a coordinator
  event) when a worker or reviewer run ends with the root's checkout on another branch than as it
  started, or with tracked paths newly uncommitted. One alert per change, not per run that saw it:
  it blames the runs that worked in the project root (else the run that ended) and lists the rest
  as also running; it clears once the checkout is back and those paths are clean. A root left
  detached or on another branch is first put back on its branch when no other run works there and
  no rebase or merge is under way: a detached HEAD's commits are kept on `rescue/root-run<id>-<sha>`,
  nothing is reset or forced, and a coordinator event says so instead of the alert. Uncommitted
  tracked changes there block the repair (an alert instead: they are never carried or discarded).
  A run whose working directory was the root, that newly dirtied tracked paths there and hands
  off `done` with no other root run alongside, is first sent back once to commit or revert them
  (no attempt spent, no alert); its next hand-off is accepted, with the alert if they are still dirty.
  Before a worktree is branched from the root, the daemon checks its HEAD the same way against
  `worktree.root_branch` (unset: the branch it was last seen on with no root alert open; a user's
  own switch is adopted): put back when safe and reported low, else one `root-head` alert that
  clears once it is back. Meanwhile new worktrees start from the base or that branch, never the
  detached HEAD; with neither known they wait with that reason.
  Code tasks always get their own worktree (a project root that is no git repo: the worker makes
  one named after its task id; two live tasks never share a branch or worktree: dispatch holds the
  later one until the other ends); `worktree.kinds` (default `[]`) gives other kinds one
  too (say `["work"]`; never harness). A review runs in the worktree holding the branch it reviews,
  else in a detached worktree of its own at the reviewed head (`worktrees/t<id>`), never the root. Off by default because a fresh worktree lacks the
  root's untracked build trees, outputs and submodule checkouts that work tasks often use (no check
  tells them from disposable ones); work tasks in the root must leave its HEAD alone. Such a
  task's branch gets the local-only, backup and integrity checks of a code task, but no review.
- Coordinator failing repeatedly → an alert says so; messages are kept, not lost.
- A worker that produces nothing for too long is stopped and retried (stall guard).
