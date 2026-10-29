---
"tt-project": patch
---

Daemon: the children the tick waits on inline (a device runner restart's `ttp devq start`, a precheck's
`ttp checks --detach`, the first healthy tick's harness `git rev-parse`) run in a session of their own,
killed whole after under a minute, so a hung one never holds a tick step past its budget or leaves a
stray process. A device runner restart is named in `tick_step_slow`.

Finishing a run no longer reads git inside its write transaction: a failed review's area and the
follow-ups' file overlaps and overlap chain (the base, the tracked and base files and the per-symbol
`git grep`, for the spec the daemon's own continue hop is queued with: the follow-up's plus the parent's
branch brief), and for a done code task its split check and its continue hop's progress and branch
brief, are read just before it (gitmemo) and reused inside, so a slow repository no longer holds the
database lock. Tasks share a probe run only when its command, directory and
environment match and the run started once their own wait began; each keeps its own verdict and output
note. A probe's raw result (killed on its timeout, killed by a signal, its last output) is kept beside
its verdict and shown in a broken probe's wake reason and its hold_probe_broken event; a hold still
ends only on exit 0.
