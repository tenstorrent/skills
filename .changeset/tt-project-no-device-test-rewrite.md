---
"tt-project": patch
---

`tt-project`: on a device host, a run that holds no device no longer loses a call when it runs pytest or ctest outside `ttp sandbox`. When the test run is the command's last part (after `;`, `&&`, `||` or a new line, heredoc bodies included), the worker hook rewrites it to `ttp clip -- ttp sandbox -- <cmd>` (PreToolUse `updatedInput`, one line of added context) instead of refusing it; every other byte of the command stays as written. A command that changes TTP_NO_DEVICE (`TTP_NO_DEVICE=`, `unset`, `env -u`, `env -i`), a test run inside a pipeline, subshell, loop or `sh -c`, and a CLI older than Claude Code 2.0.10 (the first to apply `updatedInput`; Codex and Cursor too) are still refused. Each rewrite and each such refusal is a row in the run's refusals.jsonl (`rewrite`, `no_device`).
