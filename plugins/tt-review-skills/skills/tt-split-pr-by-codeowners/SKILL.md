---
name: tt-split-pr-by-codeowners
description: Decide whether a wide-ranging pull request should be broken up so each piece needs fewer CODEOWNERS approvals, and propose how. Use for 'too many reviewers on this', 'can this be broken up', 'why does this need six approvals'.
disable-model-invocation: false
metadata:
  tier: process
  upstream: []
---

# Split a PR by CODEOWNERS approvals

Given a pull request, work out who has to approve it and propose a split where each piece needs
fewer of them. Reads the PR through `gh`, which is allowed on the review path because every Actions
runner ships it authenticated. A workflow pinning this skill must allow `gh` and `python3` in its
`tools.bash` list.

**It plans. It does not execute** — a proposal, then it stops. No branches, no pushes, nothing
written to GitHub. `references/executing-the-split.md` has the recipe for whoever carries it out.

## Why approvals and not diff size

A large diff is not automatically a hard review: what costs a reviewer is the number of independent
decisions held at once, and a 5,000-file mechanical rename is one decision. Size flags exactly the
PRs that do not need splitting. Blocking approvals are computable, and each is a person who must act
before the PR lands — on tt-metal, `.github/CODEOWNERS` is ~573 rules across ~160 owners.

**Count approvals, not owners.** Owners on one rule are *alternatives* — GitHub requires "an
approval from any of the owners", not all. A PR matching 36 owners can be unblocked by 7 approvals,
and splitting on the larger number buys nothing. `scripts/codeowners_map.py` reports both, and takes
the branch's `required_approving_review_count` as a floor: coverage is not the only gate.

## Constraints — lead with these; they settle most cases before any analysis

- Every slice must **stand on its own twice over**: green when merged alone, and coherent as one
  idea to whoever reads it. A lower approval count never buys an incoherent PR.
- **A behaviour change ships with its test.** Never separate them to shed a reviewer.
- When a later slice consumes something an earlier one renames, whatever keeps the old spelling
  working belongs in the **earlier** slice, with the dependency stated in both.
- **Recommending no split is a valid, expected outcome** — say so plainly when the PR is one
  decision, or its owner sets overlap enough that a couple of approvals already cover everything.
- **Price every proposal**: N× CI, N× review latency, a rebase chain. Argue against your own split
  when the cost exceeds the saving. On an open PR the work already exists, so the bar is higher
  than it would be at planning time.

## Workflow

### 1. Gather, keyed off the base branch

```bash
gh pr view <n> --repo <owner>/<repo> --json baseRefName,changedFiles,title,author,reviewRequests
gh api repos/<owner>/<repo>/pulls/<n>/reviews --jq '[.[]|select(.state=="APPROVED")|.user.login]|unique'
```

**Everything downstream keys off `baseRefName`** — ownership, protection and merge base all resolve
against the PR's base, which is often not `main`.

### 2. Fetch inputs without truncating them

`gh pr view --json files` silently caps at 100, and this skill's target is PRs larger than that.
Paginate, and fetch CODEOWNERS from the base branch by GitHub's location precedence —
`references/fetching-pr-data.md` has the commands and the other ways to get this wrong.

### 3. Resolve ownership

```bash
<paginated file list> | python3 scripts/codeowners_map.py \\
  --codeowners CODEOWNERS.base --expect-files <n> --required-approvals <count> \\
  --exclude "@<pr-author>" --approved "<logins who approved>" --json
```

**Pass the PR author to `--exclude`.** GitHub never accepts the author on their own PR, so leaving
them in the pool yields a minimum that cannot happen — for rules `{author, A}` and `{author, B}`
the author alone appears to cover both at a cost of one, when the answer is two.

**Pass everyone who has approved to `--approved`.** The cover is a property of the PR;
`approvals_outstanding` and `files_still_blocked` are what someone actually wants when they ask why
a PR needs so many reviewers. Often the honest answer is "it does not any more — go ask one person."

Do not read CODEOWNERS by eye — last-match-wins, owner alternatives and empty-owner resets are all
silent when got wrong. See `references/codeowners-semantics.md`, and
`references/is-it-enforced.md` for whether the base branch requires code-owner review at all.

### 4. Sanity-check, do not "validate"

Compare against `reviewRequests` as a smell test only; it proves nothing either way. Account for
every difference or call the parse unverified.

### 5. Propose, then stop

Cluster by owner set, merge clusters that add no new approver, stop once the next split saves
nothing. Wide mechanical refactors take expand / migrate / contract, with migrate batches cut along
owner-set boundaries. Strategy and the overriding rules are in `references/split-strategy.md`.
Output the proposal and hand it over; create nothing.

## Output

```
## Current

<n> files on base <branch>. <m> owners matched, but <a> approvals would unblock it: <who>.
Already approved: <who>. Still outstanding: <k> — <who> — blocking <j> of <n> files.
<If the branch floor binds, or a rule is owned only by ineligible people, say so.>
Cross-checked against reviewRequests: <agrees | differs, because ...>

## Proposed

### PR 1 — <what it does, as one idea>
Approvals: <who>       (was <a>, now <recomputed for this slice, floor included>)
Files:     <paths, or a pattern plus a count>
Depends on: —

### PR 2 — <...>
Depends on: PR 1 — <the symbol or shim that creates the dependency>

## Cost

<N>x CI, <N> review cycles, a <N>-deep rebase chain.

## Recommendation

<Split, or do not split, and why — in one sentence.>
```

## When this does not apply

Say so and stop: a couple of approvals already cover every file; the change is one decision spread
wide, so splitting yields N PRs that must all land together; the base branch does not enforce
code-owner review; or no split reduces the approval count, because ownership does not align with a
seam here.
