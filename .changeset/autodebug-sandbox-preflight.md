---
"tt-autodebug": patch
---

Check Codex sandbox startup before launching a model. Keep the sandbox when it works; stop on
failure unless the user/operator enabled `AUTODEBUG_ALLOW_UNSANDBOXED=1` for this environment and
the error is a recognized Linux sandbox startup failure. Warn before that unsandboxed fallback.
Keep fresh-process isolation and Claude's existing permission mode.
