# Machine change ends

`ttp machines change add ... --until-task <id>` keeps a ledger entry (such as a staged build tree on a shared machine) open while a dependent task of the same project is open.
It is overdue once that task is done, failed, cancelled or gone, and at the latest after 7 days.
A continuation task that still needs it re-extends it as its first step: `ttp machines change edit <cid> --until-task <its own id>`.
The daily review lists open entries whose task is over.

An `--until-probe` must still fail when the change is recorded: `add`, and `edit` given a new `--until-probe`, run it once (as the daemon would, at most 10 s) and refuse one that exits 0, since the entry would be overdue at once; give a probe that stays false until the change should end, or use `--expires` (`--allow-met-probe` stores it anyway, with a warning).
A probe that times out, cannot run or exits with a broken code is recorded with a warning naming the result.

`--auto-undo` (on `add`, or `edit <cid> --auto-undo`) lets the project's own daemon run the undo once the entry is due, instead of paging someone to run it.
Use it only for an undo that is idempotent, quick (well under 5 min) and safe to run unattended, such as removing a scratch directory or re-enabling a timer; leave it off for anything that needs judgment or touches running jobs.
The undo runs once, in its own process group, with no stdin; exit 0 closes the entry, 1, 75 or 255 retry 5 min later (3 tries), and anything else, a timeout or an interrupted run pages with the output's tail.
It waits, unpaged, while the machine is under an outage, a fleet hold or a resource pause.
`add` and `edit` refuse it for a no-op undo, an undo that kills by name or pattern, an until-probe that always exits 0 and an `--until-task` end.

A worker whose sandbox keeps the user's tt-project home read-only still runs `ttp machines change add` and `close` as usual: the CLI then queues the request in the project's own state, and the project's daemon records it within a few minutes (`ttp machines change list` shows it as queued until then).
Running the command again adds nothing. `ttp machines change edit` is not queued (close the entry and add a new one), except `edit <id> --test` or `--no-test`.

Closing your own machine-change entry (with or without an undo) is routine bookkeeping: it needs no upstream note to tt-project. `ttp upstream --fp` is only for a tt-project harness fault; `ttp note --to` only when another project's work needs a request or an answer.

An `--expires` (or `--power-cycle-at`, `ttp machines outage --expires`, a note's or a pause's `--until`) takes a delay (`6h`, `3d`) or an ISO time.
A time with an offset or `Z` keeps it; a zone-less one (`2026-10-26T15:30`, or a date alone, its midnight) is read in the project's home zone, or with no project in hand the account's `budget.timezone`, else this machine's zone, never UTC by default.
The confirmation and `ttp machines change list` show the stored end in the home zone with its abbreviation (`15:30 PDT`), so a wrong reading shows at once.
