---
"tt-project": patch
---

A revert that a later commit undid no longer makes a commit read as not landed or not live: a revert of that revert, a "Re-apply A..B" (or "Re-apply <sha>") commit naming the commit or its range, or a later commit with the same patch-id (a cherry-picked re-land) cancels it, as long as that later commit was not itself reverted. Only a revert that still stands leads to the reverse-apply test, so a re-landed change that a later batch commit edited reads landed. The dropped-landing sweep and `ttp landed --task` now share one rule: a task whose recorded supersession is on the push branch and carries its change is not reported as dropped, even when its earlier push queue landing was reverted.
