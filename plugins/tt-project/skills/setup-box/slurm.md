# setup-box: Slurm

Other people share the cluster's nodes. Hold a node only while work runs on it.

## Rules

- Get nodes only through the scheduler (`sbatch`, or `srun` for one command). Never ssh into a
  node you hold no allocation for, and never start work on one that way.
- Never take a node that is allocated, drained, reserved for others or down, and never cancel,
  preempt, join or share another user's allocation.
- Submit the whole run as one batch job that ends itself, with `--time` set to the expected run
  time plus a small margin, never the partition maximum. No interactive or no-shell allocation
  (`salloc --no-shell`) kept for later steps, and no job that waits on another task, a review, a
  tunnel, a laptop or a human.
- The job script starts every process it needs and kills them all before it exits, so releasing
  depends on nothing outside the cluster.
- Hold nothing idle: an editor, `tmux` or agent session on a node is not using it. Release the
  node as soon as the work ends, and check afterwards that nothing of yours still runs there.
- A project's charter may narrow these rules (which nodes or partitions, how long a node must have
  been idle); `box.py slurm` takes those as `--allow` and `--min-idle`. Without them it applies
  only the rules above.

## How the cluster tells you what is free

| Need | Command (on the login node) |
|---|---|
| Partitions, their nodes and states | `sinfo -N -o '%N %P %T'` |
| One node: state, last busy time, reason | `scontrol -o show node <node>` (`State=`, `Reason=`, last busy time) |
| Reservations (maintenance, demos, other teams) | `scontrol -o show reservation` |
| Limits: default and maximum time, preemption | `scontrol show partition <partition>` (`DefaultTime`, `MaxTime`, `PreemptMode`) |
| Your jobs | `squeue -u "$USER"` |

- Take a node only when its state is plain `IDLE`. `MIXED`, `ALLOCATED`, `COMPLETING`, `PLANNED`
  or any `+DRAIN`, `+RESERVED`, `+MAINT` or `+NOT_RESPONDING` flag means it is not free, and so
  does a suffix such as `IDLE*` (not responding), `IDLE~` (powered down) or `IDLE$` (maintenance).
- A node listed in any reservation is not free, even when it shows `IDLE`.
- A default time limit may be short (often an hour); always pass `--time`.
- With `PreemptMode=CANCEL` a higher-priority job may end yours; resubmit it.

### Last busy time

`scontrol show node` gives the last time Slurm saw the node busy. Many unrelated nodes sharing one
last busy time, to the second, usually means a controller event (a restart or reconfigure), not
jobs. A `--min-idle` check against it is then conservative (it may refuse a node that has been free
longer), never unsafe. Where accounting is on, `sacct -N <node>` shows when the last real job on
the node ended.

## Chips: one-chip and four-chip work

How chips are allocated depends on the cluster:

- With GRES for chips (`scontrol show node` shows `Gres=`), request them as the cluster documents.
- Without GRES (`Gres=(null)`), and with nodes reporting placeholder CPU and memory, the unit of
  allocation is a whole node: use `--exclusive`. The chip count is then a property of the node's
  board type and partition, not something a job requests.

| Work | Whole-node cluster |
|---|---|
| One chip | an exclusive node of the smallest board type, with `TT_VISIBLE_DEVICES=0` (or the test's mesh setting) limiting the run to one chip |
| Four chips | a node of a 4-chip board type, or 4 chips of a larger node (`TT_VISIBLE_DEVICES=0,1,2,3` or the test's mesh setting) |
| A whole board | a node of that board type, no device limit |

The site file maps each partition to its chips and board (`site.md`). On a new cluster the chip
count is not known until a job measures it: leave `chips` out or set it to `"unknown"`. `site check`
then warns instead of failing, and `check` and `submit` still work. A job on such a partition first
writes the number of devices in `/dev/tenstorrent` to `job.chips` and logs `tt-smi -ls`; `status`
prints the count once the job ended. Record it in the site file. A card with two chips behind one
device (such as n300) counts once, so read the `tt-smi -ls` output in `job.log` before recording:
count the rows of its "All available boards" table, not the `/dev/tenstorrent` entries (a node of
32 chips may show 33 entries there).
Verify a recorded mapping the same way in the first job on each partition, and fix the site file
if it differs.

## Whole-node exclusive partitions

Some clusters lend whole Tenstorrent boards through exclusive partitions (`OverSubscribe=EXCLUSIVE`):
a job gets a whole node, nodes report placeholder CPU and memory and no GRES, and the partition's
board type decides the chips (the table above).

- `box.py slurm submit` without `--node` lets Slurm place the job on a free node of the partition.
  Set `"exclusive": true` on such partitions in the site file; `submit` adds `--exclusive` only when
  the partition does not already enforce it.
- The default time limit may be short and the maximum unlimited: always pass `--time`. Preemption
  may cancel a job: resubmit it, or run a long set of tests as `box.py slurm cases`, which resubmits
  itself without a bad node and carries on where it stopped.
- Accounting may be disabled ("Status after a job ends") and node-local disk wiped around each job
  ("Storage").

## box.py slurm

`<plugin-root>/skills/setup-box/scripts/box.py slurm ...` runs every Slurm command on the box's
login node (`slurm.login`) over ssh.

| Command | What |
|---|---|
| `survey <box> [--partition P] [--min-idle MIN]` | read-only (`sinfo` and `scontrol` only, never `sbatch`, `srun` or `salloc`): per partition (default: the site file's), node counts by state, every plain `IDLE` node with its last busy time, minutes since then and why it is not takeable (a reservation, `demo_nodes`, `excluded_nodes`, under `--min-idle` when given), and the reservations on its nodes; ends with a note for each last busy time that several nodes share |
| `check <box> <node> [--partition P]` | exit 0 only when the node is plain `IDLE`, in no reservation, not in the site file's `demo_nodes` or `excluded_nodes`, and in partition P; also checks `--allow` and `--min-idle` (minutes since the node was last busy) when given |
| `submit <box> --partition P --time T --name NAME --checkout PATH --purpose TEXT --command CMD [--node N] [--devices 0]` | writes `job.sbatch` under `<workdir_root>/runs/<NAME>` and submits it. Without `--node`, Slurm places it: `sbatch --parsable --nodes=1 -p P --time=T`, with `--exclude` for the site file's `demo_nodes` and `excluded_nodes`, and `--exclusive` only when the site file marks the partition `exclusive` and the partition does not already enforce it (`OverSubscribe=EXCLUSIVE`); it prints `node: assigned by Slurm` and `status` shows the node once the job runs. With `--node N` it runs `check` first and submits with `sbatch --parsable --exclusive -w N -p P --time=T`. Prints the job, a `note:` line and a `retry_when:` line; on a partition without a measured chip count the job also measures it |
| `status <box> <job> [--name NAME]` | the job's state from `squeue` while it lives; after it ends, its `job.rc`, the measured chip count when the job took one, and the last 20 lines of `job.log`; exit 2 when `squeue` fails (state unknown: never treat that as ended) |
| `verify-clean <box> <node> [--ssh]` | exit 0 when no job of yours is on the node; with `--ssh`, also lists your processes there |

`survey` takes no threshold of its own: an idle minimum is site or project policy, passed as
`--min-idle` only when the charter sets one. By default let Slurm place the job (`submit` without
`--node`). Where the charter names nodes or an idle minimum, use `survey` to pick a node, then
`check` it or `submit --node` it with `--allow` and `--min-idle`: the survey is a snapshot, and
`check` runs again right before `sbatch`. `--allow` and `--min-idle` need `--node`.

`submit` refuses a missing or unlimited `--time`, `salloc` and `--no-shell`, a checkout or run dir
outside `workdir_root`, a partition missing from the site file, and any `--node` that `check` refuses.

The job script it writes:

- runs in its own process group, `cd`s into the checkout on shared storage and runs the command
  (with `TT_VISIBLE_DEVICES` when `--devices` is given);
- traps `EXIT`, `TERM`, `INT` and `HUP`: it kills its whole process group, then writes the exit
  code to `job.rc`; the output goes to `job.log`.

After submitting:

1. Run the printed `note:` line (cluster, job id, node, start, limit, purpose), and list the job in
   the hand-off.
2. Hand off `waiting` with the printed `retry_when`: it exits 0 once `squeue` no longer lists the
   job, 1 while it does or `squeue` fails, 255 while ssh fails.
3. On waking, `box.py slurm status <box> <job> --name NAME`. A time limit or preemption sends TERM,
   which the trap records as `rc 143`. `rc missing` means a SIGKILL before the trap ran, a node
   failure, or a job that never started: quote the log tail and resubmit or report it. Exit 2
   means `squeue` failed and the job may still run: retry later, never resubmit on it.
4. `box.py slurm verify-clean <box> <node>`, and record the end time with `ttp note`.

## Build once, test in parallel

For a large build (tt-metal and the like) and many tests against it, build once per checkout and
run the tests as separate jobs on that build. Both are plain `sbatch` batch jobs with a time limit,
placed by Slurm under the same rules as `submit` (the site file's `demo_nodes` and
`excluded_nodes`, `--allow` for the charter's node list):

| Command | What |
|---|---|
| `build <box> --partition P --time T --name NAME --checkout PATH --purpose TEXT --command CMD` | one job that runs the build under an exclusive `flock` on a lock in the checkout's git dir. It records the tree it built (`HEAD` plus a checksum of `git diff HEAD`) and skips the command when that tree is already built: `build: reused`. Prints a `next:` line for `test` |
| `test <box> ... --after JOB --command CMD [--command CMD ...]` | one job per `--command` (named `NAME-1`, `NAME-2`, ...), each started only once the build job succeeded (`--dependency=afterok:JOB`). Each holds the build lock shared, so tests run side by side but never during a build, and refuses (exit 96) when the tree is not the one built: rebuild first |
| `cases <box> ... --cases FILE [--chips N] [--devices D] [--fresh]` | one resumable job for a list of cases (below) |
| `verdict <box> <job> --name NAME` | the job's verdict without reading its log (below) |

Where the site file sets `slurm.container_run` (a command prefix that runs one command inside the
site's image), `build`, `test` and `cases` run their commands through it; `--no-container` runs
them on the node directly. The prefix and image come from the site file, never from a default.

### Resumable case jobs

`--cases FILE` lists one case per line: `<id><TAB><command>` (`#` lines are comments; ids are plain
names, each once). The job:

1. checks the node first: fewer `/dev/tenstorrent` devices than the partition's chips (or
   `--chips`), or a failing `tt-smi -ls`, marks the node bad;
2. runs each case in turn, logging it to `<rundir>/cases/<id>.log` and appending
   `PASS|FAIL rc=N <seconds>s <node> <job> | <id>` to `<rundir>/results.tsv`;
3. skips cases already in `results.tsv`, so a preempted or resubmitted job carries on where it
   stopped. Resubmitting the same `--name` keeps the results; `--fresh` starts over.

On a bad node it appends `<node> <date> <why>` to the bad-node list (`<slurm.state_dir>/bad_nodes.txt`,
default `<workdir_root>/state`), naming the node by its Slurm node name (`SLURMD_NODENAME`, the
hostname only outside Slurm) and dropping entries older than the bad-node window as it rewrites the
file. It then resubmits itself with that node added to `--exclude`, at most 3 attempts in all
(exit 98 after a resubmit, 95 when out of attempts). The resubmitted job writes to the same
`job.log`; `status` and `verdict` print `verdict: resubmitted; rc 98; ...; resubmitted as job N`
(`verdict` exits 2, not final): follow that job. New `build`, `test` and `cases` jobs also exclude
nodes marked bad within `--bad-days` (default 7). sbatch refuses a job whose `--exclude` names a
node it does not know, so every `--exclude` (from `submit` too) keeps only nodes
`sinfo -p <partition>` lists; when sinfo lists none, recorded bad nodes are left out and only the
site file's nodes are excluded.

### Verdict: read it, not the whole log

`box.py slurm verdict <box> <job> --name NAME` reads the job's state (`sacct`, else `scontrol`),
its `job.rc` and the end of its `job.log`, and prints without a model:

```
verdict: failed at step test; rc 1; FAILED
pytest: 1 failed, 9 passed in 3.20s
failed tests: tests/test_x.py::test_a
error: E       AssertionError: 3 != 4
```

The verdict is `passed`, `failed`, `timeout`, `cancelled` or `unknown`; the step comes from the
`==> <step> start` and `==> <step> done rc=N` lines the job scripts write; at most 5 distinct error
lines follow. Exit 0 passed, 1 otherwise, 2 while the job still runs or nothing is known. `status`
prints the same lines once a job ended. Workers read the verdict and open `job.log` or a case's log
only for what it does not answer. A failed check that ran as a Slurm job prints the same verdict line.

Credit: the build-once and parallel-test flow follows the tt-sbatch workflow; resumable cases, the
node check, the bad-node list and resubmits follow the idea of tenstorrent/tt-blaze's Slurm job
script; the model-free verdict follows the idea of tenstorrent/tt-slurm-cluster-mcp's log
parser. Neither repository grants a licence we could find, so no code was copied: these are our own
implementations of the ideas.

## Status after a job ends

- Where accounting is enabled, `sacct -j <job>` gives the final state. Where it is disabled
  (`scontrol show config` shows `AccountingStorageType=accounting_storage/none`), `sacct` does not
  work: the job's own `job.rc` and `job.log` are the only record once `squeue` drops it.
- `scontrol show job <job>` works only while the job is alive or shortly after.

## Storage

- Keep the checkout, build tree, `job.log` and `job.rc` on shared storage (`workdir_root`).
- A node's local disk may be wiped when a job starts or ends (Prolog/Epilog), including your files
  in `/tmp` and `/dev/shm`. Use node-local scratch (`scratch_root`) only for data the job copies
  back to `workdir_root` before it ends.
- The login node's home may be its own local disk, not the shared storage the nodes see.

## Checking a node afterwards

- On many clusters ssh to a compute node is allowed only during your own allocation. Then
  `verify-clean` can check only that no job of yours remains; it says it cannot check processes.
  Say so in the hand-off.
- Where ssh to nodes is always allowed, `verify-clean --ssh` also lists your processes there.

## Reaching the login node

- The login route may run through a tunnel from the user's laptop, which is down while the laptop
  sleeps or is offline. Then ssh exits 255.
- Do not wait or retry in a loop: hand off `waiting` with a `retry_when` that probes ssh, for
  example `ssh -o BatchMode=yes -o ConnectTimeout=15 <login-alias> true`. A job already submitted
  keeps running; its `retry_when` also returns 255 (still asleep) until the route is back.
