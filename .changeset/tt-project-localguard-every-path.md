---
"tt-project": patch
---

`tt-project`: project-local tool guards (`guards.local`) are asked the same way on every provider path, now with tests for each: the Claude and Codex worker hooks (Codex apply_patch and MCP calls included), Cursor's per-run hook, the runner-side guard of runs without a hook, and session coordinators, whose Read, Grep and Glob calls now go through the guards' `tool_refusal` as a worker's do. A shell command names a listed guard only by whole path components (`a.py` no longer matches `data.py`). A guard that calls `sys.exit` refuses and names itself instead of escaping the check. A project.json that does not read and has no last good copy leaves the guards unasked: the daemon's config alert and `ttp doctor` now say so (not a refusal of every call, which would also stop the runs that could fix it). The local-guards doc lists each provider path and says a full-tools coordinator edits a listed guard only through a harness task.
