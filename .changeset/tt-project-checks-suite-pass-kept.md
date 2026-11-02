---
"tt-project": patch
---

ttp checks: each check's pass is recorded the moment it passes, so a call cut off later (in the rerun of the added tests, or at a later check) leaves it, and the next call on the same tree with the same command and environment runs only what is left and the added-tests rerun; the call itself counts as passed only once all of it passed.
