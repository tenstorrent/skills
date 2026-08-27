---
on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  contents: read
  pull-requests: read

engine: claude

imports:
  - shared/pr-review-base.md
  - shared/pr-diff-data-fetch.md

skills:
  - tenstorrent/skills/tt-review-core@0000000000000000000000000000000000000000
  - tenstorrent/skills/tt-review-router@0000000000000000000000000000000000000000
  - tenstorrent/skills/ttnn-op-kernel-review@0000000000000000000000000000000000000000
  - tenstorrent/skills/tt-l1-memory-review@0000000000000000000000000000000000000000
  - tenstorrent/skills/tt-model-bringup-review@0000000000000000000000000000000000000000
  - tenstorrent/skills/tt-multichip-ccl-review@0000000000000000000000000000000000000000
  - tenstorrent/skills/tt-test-coverage-review@0000000000000000000000000000000000000000

safe-outputs:
  create-pull-request-review-comment:
    max: 10
  submit-pull-request-review:
    max: 1
---

# Tenstorrent PR review

Review this pull request using the Tenstorrent domain-knowledge skills pinned above.

> **The pinned SHAs above are placeholders.** Replace each with a real 40-character commit SHA from
> `tenstorrent/skills` before using this workflow. gh-aw reports a failed skill install as a
> **non-fatal warning**, so an unresolvable pin silently degrades the review rather than failing the
> run — you would get a generic review that looks like a domain review.

## Inputs

The diff has already been fetched to `/tmp/gh-aw/agent/pr-diff.patch` by the imported
`pr-diff-data-fetch` shared config. **Read that file. Do not call `get_diff`.**

## Steps

1. **Triage.** Use `tt-review-router` against the changed paths to decide which domain skills apply.
   Delegate this to the `tt-triage` sub-agent below.
2. **Load.** `tt-review-core`, plus **at most two** of the domain skills the router selected. If the
   router selected more than two, review the two highest-risk and say in the review body which
   domains were not covered. A bounded review must not read as an exhaustive one.
3. **Review.** Apply the loaded skills to the diff. Every finding cites `file:line` plus a source,
   per `tt-review-core`'s evidence rule.
4. **Emit.** Findings only, in the shape defined by `tt-review-core`'s
   `references/output-format.md`.

## Posting

**Do not post anything yourself.** You run read-only. The workflow posts through `safe-outputs`:
inline findings become review comments (max 10), and the summary becomes a single review.

If you find more than ten issues worth commenting on, post the ten highest-severity inline and put
the remainder in the review body. Do not silently drop them.

Note that with no `safe-outputs:` block gh-aw silently auto-enables `create-issue` — which is why
the block above is explicit rather than omitted.

## agent: `tt-triage`

Classify the diff and return the skill subset. Cheap and fast — you are routing, not reviewing.

Read `/tmp/gh-aw/agent/pr-diff.patch`, extract the changed paths, apply `tt-review-router`'s path
table, and return exactly:

```
## Skills

- tt-review-core          (always)
- <skill>                 — <the path that selected it>

## Not selected

- <skill> — <why it looked relevant but is not>
```

Do not review the code. Do not report findings. Return the list.
