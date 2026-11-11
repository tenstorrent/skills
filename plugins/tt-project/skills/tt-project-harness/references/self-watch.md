# Self-watch: the daemon's watch on its host and its own process tree

Each daemon runs `Daemon.check_self_watch` (runtime `selfwatch.py`) about every 30 s, model-free:

- Host: load average against its cores, processes created per second (`processes` in /proc/stat),
  available memory and free disk under the project.
- Own tree: every process whose environment carries this project's `TTP_PROJECT` (or a `TTP_RUN_DIR`
  inside it), and the daemon with its children, grouped by `TTP_RUN_ID`: processes, spawn rate, CPU
  cores and RSS. A run's spawn rate counts only its own processes the watch sees: pids in its tree first
  seen at a sample (against the last one) or, while the host forks at or above `spawn_s`, in about 20
  quick pid listings over 1 s (environ read only for new pids), each pid counted once, as a rate over the
  time watched (the higher of its new pids per second since the last sample and its burst pids per
  second of the burst). The host's fork rate from /proc/stat is never shared out to a run.

A run past a limit at every sample for `selfwatch.window_s` (60 s) is a runaway:

| Setting | Default | Why |
| --- | --- | --- |
| `selfwatch.spawn_s` | 1000 new processes/s | a heavy build (make -j64) starts some hundreds a second; a fork storm thousands |
| `selfwatch.procs` | 3000 live processes | a heavy build or pytest -n 64 keeps a few hundred alive |
| `selfwatch.load_x`, `selfwatch.load_share` | 2, 0.5 | host load at twice its cores while the run uses half of them |
| `selfwatch.grace_s` | 5 s | between TERM and KILL |
| `selfwatch.act` | true | false only reports |

Each also reads `TTP_SELFWATCH_<KEY>`; 0 turns a limit off. A runaway is stopped: the run's supervisor gets a
STOP, each of the run's process groups whose members are all this project's gets TERM, then KILL (a group shared
with any other process gets only the owned members), the kernel drops their flock locks, the task gets one
`selfwatch_runaway` event with the reason and numbers, and the user one notify. The daemon's own processes are
never signalled; a process with another project's or no readable environment (another user's) is never ours.
Host churn at or above `spawn_s` that the runs' own counted rates do not explain is reported once per episode
as unattributed churn (in the line and the daemon log), a warning, never a stop. Known gap, accepted: a run's
storm of processes that each live shorter than the gap between two pid listings (about 50 ms) may be
under-counted, and then shows only as unattributed churn; a storm whose processes live 2 s, or that is still
running at a sample, is counted and stopped at the default limits.

The same line shows what the project holds and installed past its own host (runtime `selfaudit.py`):

- Held machines: each machine alias (`ttp machines`) a running task names in its resources gets a read-only
  probe over its own ssh route about every 2 min (BatchMode, a 5 s connect timeout, 8 s at most, from a
  thread of its own): load against its cores, processes created per second (the /proc/stat `processes`
  delta between two probes), available memory and free disk. A machine under a pause or hold (the user's,
  a shared pause or a fleet hold) is never probed; one that does not answer shows as unreachable.
- Installed entries: about every minute `crontab -l` and the systemd user unit files are read. The line
  lists the project's own (crontab lines with its `# tt-project:<folder>` tag, its daemon and watchdog
  units); an entry that appears between two reads while one of its runs ran is flagged with those run ids.
  The first read is the baseline.
- Holdings: the slot locks labelled with this project's runs, the keeper holds kept for detached jobs and
  the live detached jobs of its running runs. A stopped runaway's locks are released at once (a lock-reaper
  sweep right after the stop), and the stop text says how many of its locks were released and names any
  still held.

The snapshot is db kv `selfwatch`; its one line shows in `ttp status`, the web app and the coordinator digest's
Host section. Tests feed a fake /proc tree and a fake signal function; the suite's `env` fixture sets
`TTP_SELFWATCH_EVERY_S=0` so test daemons never scan the real host, and `TTP_TEST_NO_SELFAUDIT_READ=1`
so nothing reads the real crontab, systemd units or held hosts (tests inject their own readers).
