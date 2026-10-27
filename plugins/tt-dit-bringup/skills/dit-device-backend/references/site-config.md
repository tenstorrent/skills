# Site config

Site-specific values live in a YAML file on the user's machine, never in this repository.

Path: `$TT_DIT_SITE_CONFIG` if set, otherwise `~/.config/tt-dit-bringup/site.yaml`.

```yaml
default_backend: slurm        # optional
batch:
  submit: <submit command>
  blocking_flag: <flag>
  status: <status command>
  cancel: <cancel command>
  default_args: []
slurm:
  partition: <partition>
  account: <account>
  resources: <gres or node spec>
  time_limit: <hh:mm:ss>
  exec: srun                  # or ssh
  setup: []                   # commands to run on the node first
```

Rules:

- Read only the keys the chosen backend needs.
- If a key is missing, ask the user. Offer to write the answer to the file; do not write it
  without their agreement.
- Never copy values from this file into committed files, PR text, or reports. `run.json`
  records the backend name, not the site values.
- Teams that want shared values can keep the file in a private repository or a shared
  directory and point `TT_DIT_SITE_CONFIG` at it.
