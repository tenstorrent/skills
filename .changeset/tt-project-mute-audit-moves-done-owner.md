---
"tt-project": patch
---

`tt-project`: the daily mute audit no longer flags a throughput-class mute whose owner task finished while its end probe still names a live task (queued, waiting, blocked or running; named as `#<id>`, `landed:#<id>`, `ttp task <project> show <id>`, `--task <id>` or `ttp eta <id>`). It moves the mute to that task, stores the new owner and shows it as "task #<new> (moved from finished #<old>)". It flags the mute only when the probe is broken or names no live task.
