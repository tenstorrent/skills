---
"tt-project": patch
---

`tt-project`: Cursor runs use `--output-format stream-json` when the CLI offers it, so the stall guard sees
progress and the mid-run budget check sees spend. Codex and Cursor coordinator turns run from an empty
scratch directory, without the project's AGENTS.md or rules, and with tools off where the CLI allows it.
