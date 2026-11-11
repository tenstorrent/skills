---
"tt-project": patch
---

`tt-project`:

- docs: the provider parity table and the guide now say that Cursor worker and reviewer runs get the harness hook (`preToolUse` for shell, edit and MCP calls, `postToolUse` for live updates, `stop` for the hand-off check) and that Cursor coordinator turns get none; a coordinator.py comment no longer calls each coordinator turn a fresh session.
