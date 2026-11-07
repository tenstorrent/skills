---
"tt-project": patch
---

`tt-project`: status claims are checked against live state before they reach the user. A coordinator reply or notify (turn and session mode) and a worker `ttp notify` are scanned model-free for claims (running, started, stopped, raised, set, written, landed, deployed, pushed, restarted); plans, conditions, negations, questions and code are not claims. Each claim is checked by its `claims` entry (an `expect` probe run before the batch, read-only, 10 s each), the task or run records it names, or the live config for a setting ('daily cap raised to $500'). A false claim goes prefixed with what is true and wakes the coordinator with a high `claim_false` event; one nothing could check goes with "(unverified)". A worker hand-off claiming a live state with no artifact or command-output path reads unverified in the digest. The coordinator prompts say to check live state first.
