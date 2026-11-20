---
"tt-project": patch
---

`tt-project`: a worker or reviewer run now sizes its compact window (`CLAUDE_CODE_AUTO_COMPACT_WINDOW`, Codex's compact limit) above its fixed prompt. Before, a project with a long charter could give light runs a fixed prompt near 70k tokens, and the light tier's 100k window compacts about 33k below that, so each run compacted on its first tool result and died with "Autocompact is thrashing". The daemon now estimates the fixed prompt (prompt text / 4 plus 20k tokens for the agent's own system prompt and tool definitions) and uses the larger of the tier's `budget.compact_window_tokens` and that estimate plus 73k tokens (33k compaction margin, 40k headroom), at most 1M. Small prompts keep the tier window, a larger configured window is kept, and 0 still turns compaction off. A raised window is recorded on the run (`compact_window` in its note) and in the daemon log.
