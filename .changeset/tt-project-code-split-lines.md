---
"tt-project": patch
---

`tt-project`: a code worker whose change would pass about `chains.split_lines` non-test lines (default 200, 0 off) is told to hand it off in parts, each reviewed and landed on its own: a first part that stands alone and passes the checks, then a `code: part 2 of <topic>` follow-up with `start_when` `landed:#<task>`. It does not use a `continue:` split, whose head skips review and leaves the whole chain to one review. Kind prompts now honour their `ttp:if` blocks in worker prompts (the upstream-note rule had shown whatever `upstream.ingest` was). Baseline for the 7-day comparison (daily review, 7 days to 2026-11-05): reviews of diffs over 200 non-test lines failed 17/44 (39%), under 50 lines 3/31 (10%); diffs over 200 lines took $69 of $97 review spend. If review-plus-fix dollars per change rise, revert.
