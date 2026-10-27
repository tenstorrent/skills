# Slurm backend

The agent runs on a login or workstation host, holds a Slurm allocation, and runs commands on
the allocated node.

Status: draft. Validate each operation on the target cluster before relying on it.

- **Acquire:** `salloc --no-shell` with the partition, account, and resources from the site
  config. Record the job ID and node name in `run.json`. Hold it for the whole stage.
- **Run:** `srun --jobid=<id> --overlap` with the command, or `ssh <node>` if the site config
  sets `slurm.exec: ssh`. Log to the run root.
- **Run in background:** the same with `nohup`, recording the remote PID.
- **Health check:** run `tt-smi -ls` on the node.
- **Reset:** run the reset from `../../../shared/device-hangs.md` rule 3 on the node, after
  this run's processes have exited.
- **Kill:** kill this run's remote process groups only.
- **Collect:** the run root must be on shared storage; otherwise copy outputs back with the
  method named in the site config.
- **Release:** `scancel <id>` at the end of the stage, or keep it if the next stage starts
  straight away and the site allows it.

## Site config keys

`slurm.partition`, `slurm.account`, `slurm.resources`, `slurm.time_limit`, `slurm.exec`
(`srun` or `ssh`), `slurm.setup` (commands to run on the node before each command).

## Failure handling

| Failure | Action |
|---|---|
| Allocation expires mid-stage | Re-acquire and restart the stage from its last recorded step. |
| Node unhealthy after reset | Release, ask for a different node, note it in the campaign's attempts ledger. |
