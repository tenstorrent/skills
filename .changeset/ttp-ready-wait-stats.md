---
"tt-project": patch
---

tt-project: `ttp stats <project> --ready-wait` (and `--all` / `--projects a,b` across this machine's projects, read-only) reports how long a ready task waited for its run to start, from becoming ready (dependencies, start_after, start_when met) to the run's start: median, p90 and max overall and per hold cause (project cap, machine-wide worker limit, its own checks, budget gate, resource, dispatch delay with a free place), plus the project's host checks slot waits. Each new run records its wait and the seconds each cause held it on its run row (as the minute ready check saw it); older runs are rebuilt from task records and alert events, with their cause "unrecorded" where no event names one and resumed waits without a ready time counted apart
