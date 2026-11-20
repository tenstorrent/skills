---
"tt-project": patch
---

`tt-project`: the daemon exits within seconds of SIGTERM even while it is finishing a run. A finish that kept rolling back and retrying its rounds ran on for minutes past a stop, until the service manager's stop timeout killed it (and a deploy's start check then failed). Now a stop ends the open round before its next git call or spawn, rolls it back and leaves the run, with the runs not reaped yet, to the next daemon, which reaps it in full. Running workers, checks and detached jobs are left alone as before (the unit keeps `KillMode=process`).
