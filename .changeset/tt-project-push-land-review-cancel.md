---
"tt-project": patch
---

`tt-project`: `ttp push --land` no longer skips a project's review step. When delivery has a review step (`delivery.review_before_pr`, on by default, or a `delivery.push_branch`, whatever `review.auto` says), it lands only a commit a passed review accepted for that task, exactly HEAD; a review of an older head does not cover a newer one, and the refusal names the missing review. New `ttp push --cancel <marker>` stops a queued or running detached push before it lands: it ends the push's process group (its checks and git too), frees its locks and check scratch, marks the push cancelled and records who cancelled it. Only the task that queued the push, or the project's coordinator, may cancel it.
