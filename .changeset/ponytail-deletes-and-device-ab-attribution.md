---
"tt-project": patch
---

`tt-project`: device A/B stops the test arm only for an incident on a device the test-arm run changed (write resources; never a question, report or status task, `read:` resources, trouble on another device or a recovery or serving-again report), within the window after the run; a re-armed start skips events at or before the stop it cleared. GLM share counts incidents the same way. `ttp config [NAME] KEY --add VALUE` adds one item to a list key (deduped; other keys refused). Removed code the ponytail audit found dead or duplicated: `replay.py`, test-only functions, the coordinator stats' stateless baseline, `ttp bench fair` and unused flags, the pool-shard test env vars, the `rings.fresh_continue` lever, and the tick steps' own error catches (the daemon's step guard reports them); shared helpers now live in `ttp/util.py` and `liveness.pid_alive` (a pid owned by another user counts as alive). `ttp new --no-secrets` is gone.
