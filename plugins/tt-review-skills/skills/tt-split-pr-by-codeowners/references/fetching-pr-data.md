# Getting the inputs right

Four ways to compute a confidently wrong answer before any ownership logic runs.

## The base branch is not `main`

GitHub resolves ownership, protection and rulesets against the PR's **base** branch, and a PR may
target a release or feature branch — tt-metal#54493 targets `sadesoye/H3_attention`. Every step
below keys off it.

```bash
BASE=$(gh pr view <n> --repo <o>/<r> --json baseRefName --jq .baseRefName)
```

## CODEOWNERS lives in one of three places, on that branch

GitHub takes the **first found** of `.github/CODEOWNERS`, `CODEOWNERS`, `docs/CODEOWNERS`. Your
working tree's copy is wrong whenever the base is not your checkout, and the wrong location silently
maps against rules that are not in force.

```bash
found=""
for p in .github/CODEOWNERS CODEOWNERS docs/CODEOWNERS; do
  if gh api -H "Accept: application/vnd.github.raw" \
       "repos/<o>/<r>/contents/$p?ref=$BASE" > CODEOWNERS.base 2>/dev/null \
     && [ -s CODEOWNERS.base ]; then
    found="$p"; break
  fi
done
[ -n "$found" ] || { echo "no CODEOWNERS on $BASE -- aborting" >&2; exit 1; }
echo "using $found"
```

**Abort if nothing was fetched.** Suppressing the errors leaves an empty `CODEOWNERS.base`, which
parses as zero rules, which makes every file look unowned — "no approvals needed", stated with full
confidence, off a transient auth blip. The matcher refuses a rule-less file too, so the guard holds
even if this snippet is skipped.

**Ask for the raw media type.** The Contents API's default JSON leaves `.content` empty above 1 MB,
while GitHub loads a CODEOWNERS up to 3 MB — so a valid 1–3 MB file decodes to nothing and reads as
missing. At 3 MB and over GitHub loads none of it, and the matcher refuses rather than reporting
ownership that is not in force.

Slashed branch names need no encoding: `?ref=` is a query value, and `branches/<name>` and
`rules/branches/<name>` both resolve them raw (verified on `sadesoye/H3_attention`). If the PR edits
CODEOWNERS itself, the base copy still governs it.

## The file list truncates at 100, silently

`gh pr view --json files` builds a `files(first: 100)` query and does not paginate. Verified: a
174-file PR returns exactly 100 rows, no error, no sign anything is missing. Large PRs are this
skill's entire target, so it fails where it does most damage.

```bash
gh api --paginate "repos/<o>/<r>/pulls/<n>/files?per_page=100" --jq '.[].filename'
```

Pass `--expect-files "$(gh pr view <n> --repo <o>/<r> --json changedFiles --jq .changedFiles)"` so
the matcher aborts on a short list rather than under-counting. The REST endpoint stops at 3000
files; past that, say so rather than implying a full picture.

## Cross-check against reviewRequests — but do not call it validation

```bash
gh pr view <n> --repo <o>/<r> --json reviewRequests \
  --jq '[.reviewRequests[].name // .reviewRequests[].login] | sort'
```

A smell test, and **proof of nothing in either direction**:

- The API returns a bare team name or login; the matcher emits `@org/team`, `@user` or an email.
  Normalise both sides or every row looks like a mismatch.
- **Extra matched** names are usually benign — GitHub never asks the author, skips owners without
  write access, drops anyone who already approved. Parser over-matching looks identical from here.
- **Extra requested** names do not prove a missed rule: a human can request anyone by hand.

Account for each difference individually. If one is unexplained, report the parse as **unverified**
and name what is unaccounted for. Never infer correctness from which set is larger.
