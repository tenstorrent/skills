# Batch backend

Device commands are submitted as jobs with a submit command named in the site config (for
example `tt-sbatch`). The agent does not run on the device host.

Status: draft. Validate each operation against the site's submit tool before relying on it.

- **Acquire:** submit one job per stage that runs a script of that stage's commands, rather
  than one job per command.
- **Run:** write the command into a job script under the run root, submit it with the site's
  blocking option if it has one, otherwise poll the job state.
- **Run in background:** submit without blocking and record the job ID in `run.json`.
- **Health check:** the first line of every job script prints the visible devices.
- **Reset:** a job step that resets its own devices, if the site allows it. Otherwise resubmit.
- **Kill:** cancel this run's job IDs only.
- **Collect:** job logs are written to the run root, which must be on shared storage.
- **Release:** the job ends.

## Site config keys

`batch.submit`, `batch.blocking_flag`, `batch.status`, `batch.cancel`, `batch.default_args`.

## Failure handling

| Failure | Action |
|---|---|
| Job pending far longer than usual | Report the queue state to the user; do not resubmit duplicates. |
| Job lost or node failed | Resubmit once, then stop and report. |
