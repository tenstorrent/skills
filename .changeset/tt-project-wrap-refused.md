---
"tt-project": patch
---

`tt-project`:

- remote checks: a spill wrapper that refuses a run (a busy host) writes the file `$TTP_WRAP_REFUSED` names and exits 75; that is a host fault, so the check runs again on another host or here, never a failed check. Exit 75 without the file still reads as 1, and a real failure stays a failure; tests
