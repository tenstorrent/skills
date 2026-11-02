---
"tt-project": patch
---

ttp checks: once every check passed, each one's pass is recorded before the rerun of the added tests, so a call cut off or failed in that rerun leaves them, and the next call on the same tree with the same commands and environment runs only the rerun; the call itself counts as passed only once all of it passed.
