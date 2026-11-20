---
"tt-project": patch
---

ttp checks: a check that is one plain local pytest run is not run again when the push queue already recorded it passing on exactly HEAD's tree, with the same command and environment (env_id, its tip one of HEAD's first-parent ancestors); the output and checks.json name the push batch the pass came from. A pass the push queue carried across a rebase without running it (verdictcarry) is marked carried in its store and never reused by `ttp checks`. Every other check (a validator, a scan, an install) always runs, and the push queue still never reads the passes `ttp checks` records.
