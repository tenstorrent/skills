---
"tt-project": patch
---

Codex: measured that `codex exec resume` (0.160) keeps the first run's developer instructions and drops a changed `-c developer_instructions`, so a resumed Codex coordinator session keeps getting the system prompt's diff in its "Changed since your last wake" block. The code comment and the provider matrix now state this as measured, and a test covers both ways a provider can take a changed prompt on resume.
