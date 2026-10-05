---
"tt-project": patch
---

`tt-project`: a resume run that fails with no tokens while the host slept is now lost to the sleep
and keeps its session for the next resume, instead of falling back to a fresh start. A resume that
fails on its own, with no sleep, still starts fresh at no attempt.
