---
"tt-project": patch
---

`tt-project`: a spilled `ttp checks` run now splits by host capability. A check that runs an agent CLI (`claude`, `codex`, `cursor-agent`, also inside `sh -c`), such as `claude plugin validate --strict`, or says `"host": "local"`, stays on the project host while the run's other checks (pytest) go to the spill host, and the run gives one verdict for both parts (`checkspill.needs_cli`, `checkspill.stays_here`). A failure of the part kept here counts at once, with no spill rerun. Spill hosts need no agent CLI or sign-in.
