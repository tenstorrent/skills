---
"tt-project": patch
---

`tt-project`: Worker and reviewer runs now compact their context before it grows large, so long runs
stop re-reading a huge context on every call. The new `budget.compact_window_tokens` sets the window
per tier (light 80000, standard 150000, deep 200000; 0 turns it off, per tier or for all). On Claude
Code it sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, which a live headless run confirmed compacts
(Claude Code raises values under 100k to 100k). Coordinator turns are unchanged. The worker prompt
now says to send big command output to a file and read only the part needed.
