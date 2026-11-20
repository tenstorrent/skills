---
"tt-project": patch
---

Lost delivery fix: a code or work task that ends with new commits on a detached HEAD (its own worktree, the hand-off's head, or a root checkout it left detached) gets a rescue branch `rescue/t<id>-<sha>` for that head, recorded as its branch, so its review can approve it for the push queue. Every review the coordinator adds quotes each covered task's "branch B, head H" when its spec does not name the branch, so review_stall replacements approve refs the push queue can validate.
