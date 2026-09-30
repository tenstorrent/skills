---
"tt-project": patch
---

`tt-project`: Codex and Cursor results are read correctly on more paths. A Codex error that the CLI
retried no longer fails a turn that then completed. Cursor's "Authentication required" is treated
as logged out, not as a failed attempt. When a Codex or Cursor run fails before writing any output,
the reason from stderr is kept in the task's result. A run that reports no usage (every Cursor run,
and a failed Codex turn) is charged the elapsed share of its budget instead of $0, so the spend caps
count it.
