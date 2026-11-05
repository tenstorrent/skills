# CI compute for goal 12

Optional. The stage runs on one machine by default and must work without this. Read it only when
`compute.kind` is `github_actions`. Runners do not share your filesystem — they fetch the
implementation from Git refs — so a grant needs a publish step as well as a dispatch step. Small
and fast checks can stay on the local device; CI is for what needs the grant.

## Input

```json
{
  "compute": {
    "kind": "github_actions",
    "repo": "<owner>/<ci-repo>",
    "workflow": "<workflow-file.yml>",
    "max_parallel": 6,
    "run_timeout_hours": 14,
    "runners": [
      {"runner_label": "<label>", "device_type": "<device>", "count": 6}
    ],
    "dispatch_inputs": {"workflow": "agentic", "run-full-evals": false},
    "publish": {
      "tt_metal": {
        "remote": "<url>",
        "branch": "<branch>",
        "base": "<pinned upstream launch commit>"
      },
      "tt_inference_server": {"remote": "<url>", "branch": "<branch>", "base": "<ref>"}
    }
  }
}
```

The numbers are an example of the shape, not values to copy. `dispatch_inputs` must match the workflow's declared `workflow_dispatch` names. Each distinct
`runner_label` is a machine class to prove separately, not a duplicate.

## Publish

**tt-metal**: the implementation as one commit on the pinned base — `models/autoports/<model>/`
source and what it imports. No `doc/`, perf CSVs, profiler dumps or collected results; every run on
every machine clones this ref. **tt-inference-server**: what the harness needs to serve and score,
same basis.

Build both in a separate checkout. The execution checkout keeps no remote — a clean-room lane
blocks transports deliberately.

Dispatch by **SHA, not branch**. A branch moves, and a run starting after the next push measures
the wrong implementation while reporting a plausible number. Check which repository the workflow
clones: a ref input with no matching repository input means the canonical upstream, so a fork is
unreachable.

Confirm the tree before spending a machine-hour on a missing file:

```bash
git ls-tree -r --long "$SHA" -- "models/autoports/$MODEL" | sort -k4 -n -r | head -5
```

The largest blobs should be source.

tt-inference-server ignores `*.json`: force-add any new config file the server reads, and confirm
from the server log on the first run that it loaded.

## Dispatch

```bash
gh workflow run "$WORKFLOW" -R "$REPO" \
  -f model="$MODEL" -f runner-label="$RUNNER_LABEL" -f device-type="$DEVICE_TYPE" \
  -f tt-metal-git-ref="$TT_METAL_SHA" -f inference-server-git-ref="$TIS_SHA" \
  -f workflow=agentic
```

Use the granted workflow as it is, with its default setup and cleanup; do not fork, wrap or patch
it. Other users share the runner pool: `max_parallel` is a ceiling, not machines waiting idle, so
jobs may queue. Queueing is not a failure; never cancel a run only because it has no runner yet.
Dispatch the first CI smoke in the first hour so the wait overlaps local work, and keep up to
`max_parallel` runs in flight after it: one suite per runner, rung runs and effort samples side by
side. Samples may run while the smoke is pending and count once it passes.
When the stage stops for any reason, its dispatched CI runs keep running; collect their results on
resume instead of cancelling them.

No run id is returned: poll for the new run and register it before dispatching the next. The ledger
is shared state when several explorers dispatch — register at launch, not at collection. Count
`queued` + `in_progress` against `max_parallel`. Collect into `doc/agentic_evals/runs/<arm>/` with
the prediction, dispatched inputs, published SHAs and run id.

`compute.run_timeout_hours` is the CI job limit, enforced by CI, not by you: a run that reaches it
is killed and returns nothing. When it is lower than the stage's `run_timeout_hours`, it is the
limit for runs on CI. Use it as a planning bound — do not dispatch what the top of its estimate exceeds. Split
suites on task boundaries so parts stay scorable, and give a multi-eval run per-eval timeouts
summing under the cap.

Infrastructure failures (runner offline, image pull, full disk, unrelated timeout) in rungs and
efforts are re-dispatched, not scored. Crashes, loops and malformed output are measurements. The
release run is planned to land without re-dispatch, as the skill's stage order says, on a route
proven in the preflight and rehearsed.
