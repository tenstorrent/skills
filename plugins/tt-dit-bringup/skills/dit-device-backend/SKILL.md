---
name: dit-device-backend
description: "Runs device commands for tt-dit-bringup on the right machine: on the local host, through a batch submit command such as tt-sbatch, or inside a Slurm allocation reached over ssh/srun. Resolves which backend to use, reads site details from a local config file, and asks the user when it cannot decide. Use before any tt_dit bring-up command that touches a device."
---

# Device backend

Every device command in a tt_dit bring-up goes through one backend. A backend is a mode file in
`references/` that implements the contract in `references/contract.md`.

| Backend | Use when | Mode file |
|---|---|---|
| `local` | The agent runs on the device host | `references/local.md` |
| `batch` | Jobs are submitted with a blocking or polling submit command | `references/batch.md` |
| `slurm` | The agent holds a Slurm allocation and runs commands on the allocated node | `references/slurm.md` |

## Choose the backend

Stop at the first rule that gives an answer:

1. The user named a backend for this run.
2. `run.json` in the run root already records one (resume).
3. The site config names a default (`references/site-config.md`).
4. Ask the user. Offer only the backends the host can support: `local` if a Tenstorrent device
   is visible, `batch` if the site config names a submit command, `slurm` if `sinfo` is on PATH.

Do not choose a backend by detection alone. A host can see a device and still be the wrong place
to run. Record the choice in `run.json`.

## Site details stay out of this repository

This repository is public. Hostnames, partition names, accounts, reservation names, internal
tool paths, and module names belong in the user's site config, not in skill text, examples, or
committed run files. When a mode file needs one of these values, read it from the site config.
If it is missing, ask the user and offer to save it there.

## Rules for every backend

- Hold one allocation or job per stage, not one per command.
- Keep the checkout, environment, and run root on storage the device host can read.
- Run the backend's health check before the first device command of each stage.
- Timeouts, hang detection, and recovery follow `../../shared/device-hangs.md`; the backend
  only decides where those commands run. Record hangs in the campaign ledgers.
