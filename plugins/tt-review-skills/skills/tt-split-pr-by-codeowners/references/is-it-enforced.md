# Is code-owner review actually required?

Worth doing once per base branch, and the obvious check answers wrongly: classic branch protection
and rulesets are separate mechanisms, and a repo may use either or both.

```bash
gh api "repos/<o>/<r>/branches/$BASE/protection" \
  --jq '{owner: .required_pull_request_reviews.require_code_owner_reviews,
         count: .required_pull_request_reviews.required_approving_review_count}'
gh api "repos/<o>/<r>/rules/branches/$BASE" \
  --jq '[.[] | select(.type=="pull_request") | .parameters]
        | {owner: map(.require_code_owner_review) | any,
           count: map(.required_approving_review_count) | max}'
```

On tt-metal's `main` the first says `false` for code-owner review and the second says
`[false, true]` — two overlapping rulesets, which GitHub composes **most restrictive**. It is
required. Reading only the first says the opposite, and would make this skill look pointless on the
repo it matters most for.

**Take `required_approving_review_count` too and pass it as `--required-approvals`.** Coverage is
not the only gate: if the branch demands two approvals, a slice with a one-owner cover still needs
two, so the honest number is `max(cover, required_count)` — and N slices multiply that floor N
times. Ignoring it understates the current cost and the proposal's.
