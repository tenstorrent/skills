---
"tt-review-skills": minor
---

Add `tt-triage-review` to the `metal` bucket. It reviews tt-metal's `tools/triage` diagnostic
scripts by the principles their maintainers have applied in a year of review: trustworthy
output from a broken system, failing loudly, framework-owned plumbing, one source of truth for
hardware facts, output written for the reader, explicit and necessary code, and observing
without perturbing. The skill learns current conventions from the repository at review time
instead of hard-coding API names. The router sends `tools/triage/**` to it.
