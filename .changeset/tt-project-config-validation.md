---
"tt-project": patch
---

`ttp config`, `ttp doctor` and the daemon now report unknown keys in a project's settings (with a "did you mean" hint) and `delivery.push_checks` entries that are not runnable commands; `ttp config` refuses an unknown key. The known-key list covers every key the runtime reads, and the deprecated `budget.max_pace_hold_s` is accepted without an alert. The config alert is sent at most once a month, and the record of sent alerts is now kept that long. The web app's settings endpoint answers 400 with the reason when a value is rejected.
