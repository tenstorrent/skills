---
"tt-project": patch
---

tt-project: `ttp checks --slot-status` (text and `--json`, new "runs" key) lists every live check run: spilled runs with their host and pid, runs holding a lane here, and every run waiting for a slot with what holds it (a parked run, the suite's test slots, or a checks slot here under the load guard); it says "none running or waiting" only when nothing runs or waits. A spill host whose tree copy timed out is kept off the rerun after that one try (a plain host fault still needs two), so the rerun moves at once to the next free spill host or this machine, and the checks log names the copy timeout as the cause; the copy timeout can be lowered with TTP_REMOTE_COPY_TIMEOUT_S and its fault message gives the size of the tree sent (git archive at the head, compressed).
