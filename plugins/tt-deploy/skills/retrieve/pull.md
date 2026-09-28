# Pull (`--pull` only)

Runs after the table, executes only what the table named, never launches.
Absent `--pull`, this file is never read.

## Preconditions

- R8 (and B13 on the B path) read `yes`, and R3/B15 credentials are met
  (`token: set` whenever a token is required).
- Disk: R4 weights + R14 compressed image (+ B11 bundle) fit the free space
  from `tt:discover` D1. Short → stop; report the shortfall; pull nothing.

## Commands

| Path | Command | When `tt` is absent |
|---|---|---|
| R image | `docker pull <docker_image>` | same |
| R weights | `$TT model pull <model_name>` | `uvx --from huggingface_hub hf download <hf_weights_repo> --exclude 'original/*'` |
| B bundle + weights | `$TT model pull <bundle id>` | `$TM pull <bundle id> --with-weights`; no `tt-model` either → `uv tool install tenstorrent`, then `$TT` |

Run in that order, one Bash call each. Caches are shared (`HF_HOME`, the
docker store), and partial downloads resume, so re-running is safe.

## Report

One line per command: `<command> → rc <n>, <bytes or 'cached'>`. Then
re-answer R14 `local` and B4 from disk.
