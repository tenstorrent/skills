---
"tt-project": patch
---

`tt-project`: the tests gate's flaky-test quarantine gets a lift path. The daemon lifts a quarantined test once a done code task whose title names it has landed on the push branch (`ttp landed --task`), and not before the head it last flaked at; a task whose spec only mentions the test does not count. `ttp quarantine lift <id> --why` lifts one by hand. Every lift is logged to `state/test_quarantine_lifts.jsonl` and shown in `ttp status` and the daily review. Nothing is lifted for looking gone: an id whose file or function is not found at the push-branch tip is only marked so in `ttp quarantine list`, `ttp status` and the audit, so a deleted quarantined test stays listed until lifted by hand. The quarantine file is written atomically.
