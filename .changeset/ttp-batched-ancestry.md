---
"tt-project": patch
---

Daemon steps answer commit ancestry in a fixed number of git calls instead of one `merge-base --is-ancestor`
per pair. A review's quoted hashes against its branch heads, the head a review covers, a rebased-away
check and the tip a head landed on now take one `cat-file --batch-check`, one `merge-base --octopus` and
one `rev-list --parents`, however many hashes, heads or reflog entries there are. `diff_lines` resolves
all its refs in one call. On a review quoting 60 hashes over 6 branches, one dispatch pass went from
439 git calls (120 of them `--is-ancestor`) to 82. A slow dispatch step now names the task it was on.
