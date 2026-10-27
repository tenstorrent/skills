# Stage summary and report formats

Written for a person reviewing the stage in a few minutes. Status: `PASSED`; `FLAGGED` (ran,
with items under "Needs review"); `BLOCKED` (a hard prerequisite is missing); `ADOPTED` (an
earlier stage checked from existing work, see `partial-runs.md`); `NOT RUN`. Keep `SUMMARY.md` to about one page.
Facts come from logs and tests in the run root; link to them rather than pasting them.

## `SUMMARY.md`

```markdown
# Stage <N>: <name> — PASSED | FLAGGED | BLOCKED | ADOPTED | NOT RUN

| | |
|---|---|
| Commit | <sha> |
| Hardware / mesh | <device type>, <mesh shape> |
| Weights | real | seeded random |
| Gate | <the gate, in one line> |

## Result
<2–5 sentences: what was done and what it shows.>

## Evidence
<the stage's table, e.g. component | shape | PCC | rel. RMSE | bar | pass>
Logs: <paths>

## Needs review
<loosened bars, approved fallbacks, surprises, decisions a person should check. "None" if none.>

## Amendments
<later changes to this stage's result, newest last, each with date, reason, and new evidence.>
```

## `PERF_REPORT.md`

```markdown
# Performance: <model> on <device type> <mesh>

| | |
|---|---|
| Commit | <sha> |
| Shape | <production shape> |
| Method | warm window: <method>; trace on/off |
| Warm device time | <ms> per <unit> (<range over N runs>) |

## Where the time goes
| Category / op | Share of warm window | ms | Notes |
<top ops or categories, summing to at least 80%>

## Tall nails
| Rank | Op / region | Share | Likely lever | Evidence |
<ranked; each lever names the profile row it comes from>

## Changes (stage 7 only)
| Round | Change | Before ms | After ms | Δ % | Quality gate |

Profile: <path to Tracy capture> · Ops report: <path>
```

## `BRINGUP.md`

One page: model, hardware, weights, one line per stage with status and a link to its summary,
the final warm device time and quality scores, and everything listed under "Needs review" in
any stage.

## `bringup_report.json`

```json
{
  "model": "<source>",
  "hardware": "<device type>",
  "mesh": "<shape>",
  "commit": "<sha>",
  "weights": "real",
  "stages": [
    {"stage": 1, "name": "components", "status": "passed", "summary": "stages/01-components/SUMMARY.md", "evidence": ["<log path>"]}
  ],
  "needs_review": ["<item>"]
}
```
