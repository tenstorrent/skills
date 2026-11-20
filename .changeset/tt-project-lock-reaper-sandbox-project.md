---
"tt-project": patch
---

`tt-project`: the stale-lock sweep now tells a reused pid from a live holder for locks taken inside a worker's sandbox, the most common worker path. Such a lock is labelled with the run's agent pid, and its label now also records the agent's start time as the runner saw it (the second line of `child.pid` in the run folder); before, it recorded none, so a lock whose agent pid had been reused was never released. A leftover process is stopped as the holder's own run only when its project matches the label's too, since run ids are unique only per project and shared lock folders hold several projects' locks. Labels from an older release (no start time, no project) are judged as before.
