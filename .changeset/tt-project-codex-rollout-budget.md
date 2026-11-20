---
"tt-project": patch
---

`tt-project`: Codex runs are priced mid-turn from the running token totals in their session rollout (`token_count` events), so the runner stops a `codex exec` run once it passes its budget, and a cut-off Codex run books the tokens it spent instead of the elapsed share of its budget. A resumed session counts only what this run added.
