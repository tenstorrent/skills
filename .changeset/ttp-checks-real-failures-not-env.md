---
"tt-project": patch
---

tt-project: `ttp checks` no longer reruns real test failures as a spill host's environment fault. A
run where tests failed on assertions beside one that failed on a missing package is the change's
failure, kept with its failing ids and not run again; the missing package is one note about that
host's checks env. Reruns are bounded across runs: an unchanged head (same commit and check
commands) is never sent back to a spill host it already failed on, and a failure at import reruns
once per head, after which the result counts.
