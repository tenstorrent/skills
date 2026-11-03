---
"tt-project": patch
---

The PR watcher reads only tasks whose pr_url is a real pull request (a `/pull/<n>` URL or `owner/repo#N`). A value such as `none` or a branch tree URL is skipped before any gh call and no longer counted as a PR, so it stops failing a gh read on every run; the responsibility inventory lists only real PRs too.
