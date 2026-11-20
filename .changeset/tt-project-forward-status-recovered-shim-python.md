---
"tt-project": patch
---

`tt-project`:

- `ttp upstream --forward-status` says `recovered` instead of `failing (Nx since ...)` once a send from a run or the CLI went through after the daemon's failures; the daemon keeps its own record, so the run fallback still works.
- A forwarded command that the project machine's older ttp rejects (exit 2, a usage error) names that machine and its ttp version (`on <host> (ttp <version>): ...`), so the error no longer reads as local.
- The `ttp` shim that `ttp setup` installs runs under a Python new enough for `ttp checks` (3.10+): `$TTP_PYTHON`, else the Python setup ran under, else the newest `python3.x` on `PATH`. With only a 3.9 it still runs and says why `ttp checks` cannot; with none, it says what it needs and exits 127.
