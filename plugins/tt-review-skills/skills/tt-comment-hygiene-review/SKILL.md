---
name: tt-comment-hygiene-review
description: Reviews comments and documentation surface — iteration-journey comments that describe how the code got here, unsurfaced tribal knowledge, magic tile and grid values, and ttnn op docstrings. Cheap to run alongside any other review. Use on any Tenstorrent diff.
metadata:
  tier: process
  upstream:
    - repo: tenstorrent/tt-buddy
      ref: ba9021417442d59756aa8cdf154a25648c9a0de5
      path: skills/code-review/reviewers/fresh-eye.md
    - repo: tenstorrent/tt-buddy
      ref: ba9021417442d59756aa8cdf154a25648c9a0de5
      path: skills/code-review/reviewers/documentation.md
---

# Comment hygiene review

Assumes `tt-review-core`. Cheap, runs alongside anything else. Two concerns: comments that should
not exist, and knowledge that should be in the code and is not.

## Iteration-journey comments

A comment describing **how the code got here** rather than what it does. It reads as helpful and
decays immediately, because the reader has no access to the state being contrasted against.

Grep the diff for the tells:

```
\bwas\b|\bpreviously\b|\bused to\b|\bvs\.?\b|\bnow uses\b|\binstead of\b|\bchanged from\b
```

> `// changed from 4 to 8 because 4 was too slow`

Six months later "4" means nothing. If the value matters, the comment should say why 8 is right —
what constraint picks it — not what it replaced. **The PR is the record of the change; the comment
is the record of the state.**

Not every match is a finding. `instead of` is legitimate when contrasting two live alternatives
("uses X instead of Y because Y forces a layout change" — both still exist and the reader can check
both). The test is whether the thing being contrasted against still exists.

## Tribal knowledge is a bug

If behaviour needs historical context to understand, that knowledge is not in the code.

- **Hardware acronyms** — DST, FPU, SFPU, NoC, CB, BRISC/NCRISC/TRISC, LoFi/HiFi, CCL, PCC — should
  not have to be guessed by a newcomer. A comment, a named constant, or a pointer to
  `tech_reports/` fixes it. The role cross-walk in particular (which RISC is the reader, which is
  the writer) is worth stating wherever it is load-bearing.
- **Magic tile, CB and grid numbers.** An unexplained `8`, a bare `CoreRange(...)`, a shard shape
  with no derivation. Either a named constant or a comment giving the constraint.
- **Data-format and sharding rationale.** *Which* format is visible in the code; *why* usually is
  not.
- **Pipeline roles** should be evident from filenames and first comments — which file is reader,
  which is compute, which is writer.

## Onboarding-path breakages

Flag patterns a newcomer would plausibly misuse even if regulars navigate them fine: implicit
initialisation ordering, silently-required environment variables, subtle API gotchas. "Everyone here
knows that" is the condition being flagged, not a rebuttal to it.

## Op docstrings

For ttnn ops, the docstring is the public surface. Check that supported dtypes, layouts, and memory
configs are stated and match the actual `SUPPORTED` declaration — see `tt-test-coverage-review` on
registry honesty. A docstring promising more than the op delivers is worse than a missing one,
because it is trusted.

## Do not flag

- **Complexity that reflects real problem complexity.** Kernel code is genuinely intricate; density
  is not obscurity.
- **Industry-standard terms** — API, JSON, HTTP — do not need explaining.
- **Absent comments on self-evident code.** A comment restating the line below it is noise; asking
  for one is worse.
- **Comment style preferences.** This is about information content, not formatting.

## Severity

`MUST-FIX` — a comment or docstring that will actively mislead into an error, including one that is
now wrong because the code changed under it. `SHOULD-FIX` — tribal knowledge not in the code, magic
values with no derivation, iteration-journey comments. `CONSIDER` — minor clarity.
