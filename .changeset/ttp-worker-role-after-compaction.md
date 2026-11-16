---
"tt-project": patch
---

`tt-project`: a worker or reviewer run gets its role and task again after its agent compacts its context, model-free (`rolerestore.py`). A run copied from the coordinator's session could compact before reading its own prompt, and the summary then spoke as the coordinator. Claude Code gets the restatement from a SessionStart `compact` hook. Every provider gets it from the run watch, which counts compactions (`Provider.compactions`: the stream's `compact_boundary`, Codex's rollout `compacted` records) and sends one update per compaction no hook covered. A task run never resumes the coordinator's live session id. A waiting hand-off's `cells` and `cells_unrunnable` carry into the final on_pass hand-off when it has none (notes ad1feca489b64a5b, 1fa82f20767cbe4c).
