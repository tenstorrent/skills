---
"tt-project": patch
---

`tt-project`:

- each listed machine's reach record: the daemon probes it with a harmless `ssh <alias> true` at most every `ssh.reach_probe_s` (default 20 min), keeps the answer and its time for every project on the host, and the coordinator's digest shows it next to the machine list. The claim check marks a reply or notify saying a machine can't be reached false when that machine answered within `ssh.reach_fresh_min` (default 30), and unverified when no probe is that recent; tests
