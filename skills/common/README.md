# `common`

Cross-cutting review discipline — the contract every other skill assumes.

| Skill | Description |
|---|---|
| [`tt-comment-hygiene-review`](tt-comment-hygiene-review/SKILL.md) | Reviews comments and documentation surface — iteration-journey comments that describe how the code got here, unsurfaced tribal knowledge, magic tile and grid values, and ttnn op docstrings. Cheap to run alongside any other review. Use on any Tenstorrent diff. |
| [`tt-perf-claim-review`](tt-perf-claim-review/SKILL.md) | Reviews performance claims rather than performance changes — whether the measurement supports the assertion. Covers device duration versus wall time, bound classification, first-run program-cache caveats, absolute versus percentage comparisons, and warmed traced harness parity. Use when a PR body, comment, or test asserts a speedup, a regression, or a throughput number. |
| [`tt-review-core`](tt-review-core/SKILL.md) | Shared review contract for all Tenstorrent code review — severity vocabulary, the evidence rule, scope, output shape, and the do-not-flag guards. Use when reviewing any change to a Tenstorrent repository (tt-metal, tt-llk, tt-inference-server, model code), and load it before any domain review skill. |
| [`tt-review-router`](tt-review-router/SKILL.md) | Maps changed paths in a Tenstorrent pull request to the domain review skills that apply. Run this first to pick the skill subset for a review. |
| [`tt-test-coverage-review`](tt-test-coverage-review/SKILL.md) | Reviews test coverage for Tenstorrent changes — PCC bars and when a lower one is justified, tile-boundary and padding cases, sharding and data-format variants, program-cache tests, and a regression test on every bug fix. Use when a change alters op or kernel behaviour, whether or not it touches tests. |
| [`tt-split-pr-by-codeowners`](tt-split-pr-by-codeowners/SKILL.md) | Maps changed files to CODEOWNERS approval groups and proposes whether a PR should be split to reduce review requirements. |

See the [top-level Reference](../../README.md#reference) for the full catalogue.
