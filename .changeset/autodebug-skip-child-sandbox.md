---
"tt-autodebug": patch
---

Replace error-string sandbox fallback with an explicit `AUTODEBUG_SKIP_CHILD_SANDBOX=1` retry chosen by the calling agent using the failure context and existing authorization. Default preflight failures stop before model launch and preserve diagnostics.
