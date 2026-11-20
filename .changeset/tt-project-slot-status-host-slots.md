---
"tt-project": patch
---

`ttp checks --slot-status` (and `--json`, under `host_slots`) gains a "host slots" section. It lists
each held host-wide checks slot (the cap's `checks.N.lock`, the push priority and the urgent slots) with
the project, task, run, pid and alive state of its live lock holder. That holder comes from the kernel's
lock table and its own process environment, never from the lock file's JSON, which keeps a released
holder's record. A released slot is no longer shown as held. Where no lock table exists, the file's
label is shown, marked unchecked. The section also lists the first parked runs waiting for their test
slots, in the order the queue admits them, with their owed, priority, landing, small and requeued tags.
