---
"tt-project": patch
---

tt-project 0.2.1004: Claude Haiku is asked only where its answer is read. Haiku answers in the background, so only a decision that asks again on a later look can use it; the wake gate does (a due wake waits one minute for the answer). Log and alert screening judges a new issue once and the wake tier is picked once per dispatch, so their Haiku answers were paid for and never read; holding them for Haiku instead would delay new alerts and urgent wakes by its 40-49 s. Both now stay with Jev alone (the rules decide while Jev is off), and Haiku decides only the wake gate. `jev.haiku.timeout_s` now defaults to 90 s, above the 40-49 s live Haiku answers took, so a slow good answer is no longer cut off after being paid for.
