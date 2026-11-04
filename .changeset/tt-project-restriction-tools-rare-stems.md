---
"tt-project": patch
---

The check for project tools that may still enforce a removed or loosened Restrictions item now treats a word as distinctive only when it is on at most 0.5% of the scanned tool lines (or 20 lines in a small tool tree), not 5%. On a large tool tree no word reached 5%, so the old cut removed nothing. Two words now match a tool line only when one of them is distinctive. A replacement text that keeps only the item's citation, such as "(User's word, 2026-10-05.)", now counts as a removal, so the item's quoted phrases and cite are matched and its common words are not.
