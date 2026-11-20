---
"tt-project": patch
---

The digest's "Temporary instructions possibly over" list and the daily review's "possibly over" line no longer read a pull request or issue number as one of the project's tasks. A `#N` after "PR", "pull request" or "issue", or after a repository name (`owner/repo#N`, `owner/repo PR #N`), is left unjudged. Before, an entry such as "until the example/repo PR #<n> is merged" showed "(now #<n>: done)" whenever task #<n> was done. An entry with its own until_probe is judged only by that probe, never by the task states its text names. A plain `task #N` or bare `#N` still shows that task's state.
