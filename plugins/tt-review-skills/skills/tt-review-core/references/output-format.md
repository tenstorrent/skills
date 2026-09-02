# Output format

Emit exactly two sections. No preamble, no conclusion, no summary of the PR's intent unless the
workflow asked for one separately.

```
## Findings

- **[MUST-FIX]** <brief title>
  - File: <path>:<line>
  - Issue: <what is wrong> — <evidence: a repo path, an invariant, or a skill reference file>
  - Suggestion: <how to fix>

- **[SHOULD-FIX]** <brief title>
  - File: <path>:<line>
  - Issue: ...
  - Suggestion: ...

## Unresolved

- <question you could not answer, and which finding it downgraded>
```

If there is nothing to report:

```
## Findings

No issues found.

## Unresolved

None.
```

## Rules

- **`path:line` as plain text, never in backticks.** Downstream tooling converts plain refs into
  permalinks; backticked refs need normalising first.
- **`Suggestion:`, not `Please`.** A review proposes; it does not order.
- **One finding per issue.** Do not bundle three problems into one bullet.
- **Never post.** No `gh api -X POST`, no `gh pr review`, no writes of any kind. The workflow owns
  posting through `safe-outputs`. Emitting a finding is the whole job.
- **The `Unresolved` section is not optional.** If you downgraded a severity because you could not
  verify something, it appears here. An empty `Unresolved` on a complex diff is a claim that you
  verified everything — do not make that claim lightly.

## Permalinks

`scripts/linkify_review.py` converts `path:line` and `path#Lline` references into
`https://github.com/<owner>/<repo>/blob/<sha>/<path>#L<line>`. Use commit SHAs, never branch names
or diff anchors — branch links rot as soon as the branch moves.

The script is optional. Findings are valid without it; it only improves the rendered comment.
