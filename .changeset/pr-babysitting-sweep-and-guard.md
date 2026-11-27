---
"tt-project": minor
---

`tt-project`: every project babysits all of its open PRs, draft or not. The pr-watch sweep (on by default,
`pr_babysit.enabled`) reports `pr_findings` for any open PR, `pr_ci_not_run` for skipped checks and listed
workflows that never ran on the head, `pr_idle` after `pr_babysit.idle_h` quiet hours, and one chat line per
status change; it posts and dispatches nothing. The `gh` guard now lets a worker run `gh workflow run
<workflow> --ref <PR head>` and `gh pr edit --add-label` only as `pr_babysit.dispatch` lists them (so a
`gh workflow run` outside an owned PR's head is refused), and ping reviewers with a comment that ends with the
AI tag. A PR in `pr_babysit.no_post_prs` gets no comment, edit, dispatch or push. The gh route's workstation
runs a dispatch and a label only on an open PR its own gh user opened.
