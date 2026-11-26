---
"tt-project": patch
---

GLM guard: a command whose program name or target comes from a plain `$(...)`, or that evals one, is no longer refused up front. The hook judges each `$(...)` body as a command, runs it, writes its output in as literal text and judges the result with the same rules; what runs is what was judged. Credential reads, other-project edits, writes outside the home and temp folders and secret prints stay refused, with the harm named.
