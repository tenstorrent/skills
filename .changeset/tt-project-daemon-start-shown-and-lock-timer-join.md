---
"tt-project": patch
---

`tt-project`: a queued task whose start time is set ahead by the daemon (a tests gate deflake batch, or any other `not_before`) now says when it starts and why, in `ttp status`, `ttp task <project> list`, the coordinator digest and the web app's task view: `starts <home-zone time> (daemon: next-day flake batch)`. A retry keeps reading `waiting, next try`. Queue-wait times already left out the time before the start; a test now covers it. `ttp lock` waits for its foreground-run reminder to finish writing before it exits, so no note is written after the command has ended.
