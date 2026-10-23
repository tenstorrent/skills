# `metal`

tt-metal host and kernel infrastructure.

| Skill | Description |
|---|---|
| [`tt-l1-memory-review`](tt-l1-memory-review/SKILL.md) | Reviews per-core L1 footprint and circular-buffer sizing — buffer inventory discipline, data-movement cost across memory tiers, CB capacity versus tile counts, and accumulator sizing. Use when reviewing program factories, CB allocation, blocking or work-split changes, or any change that adds a buffer. |
| [`tt-triage-review`](tt-triage-review/SKILL.md) | Reviews tt-triage (`tools/triage`) changes by the principles its maintainers apply: trustworthy output from a broken system, failing loudly, framework-owned plumbing, one source of truth for hardware facts, output written for the reader, explicit and necessary code, observing without perturbing. |

See the [top-level Reference](../../README.md#reference) for the full catalogue.
