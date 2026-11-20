---
"tt-project": patch
---

`tt-project`: a hung test no longer holds a checks lane forever. Each minute the daemon reads, without a model, when each running `ttp checks` run's log last grew and whether its processes still use CPU. A run whose log has not grown for `checks.stall_s` (default: that check's median passing run, at least 15 min; 0 turns it off) shows `STALLED` in `ttp checks status`, the lane wait lines and `ttp checks --result`, and records one `checks_stalled` event per stall. The daemon stops the run only once its processes have also used no CPU for that long, and it then ends as a retryable failure (reason stalled), never a pass. A slow run whose log still grows is never touched. tt-project's own tests dump every thread's stack (faulthandler, kills nothing) after 8 min; other plugins' tests are unchanged.
