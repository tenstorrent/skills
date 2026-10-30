# CI compute for goal 12

Read when `compute.kind` is `github_actions`. Runners do not share your filesystem — they fetch the
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

`dispatch_inputs` must match the workflow's declared `workflow_dispatch` names. Each distinct
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

## Dispatch

```bash
gh workflow run "$WORKFLOW" -R "$REPO" \
  -f model="$MODEL" -f runner-label="$RUNNER_LABEL" -f device-type="$DEVICE_TYPE" \
  -f tt-metal-git-ref="$TT_METAL_SHA" -f inference-server-git-ref="$TIS_SHA" \
  -f workflow=agentic
```

No run id is returned: poll for the new run and register it before dispatching the next. The ledger
is shared state when several explorers dispatch — register at launch, not at collection. Count
`queued` + `in_progress` against `max_parallel`. Collect into `doc/agentic_evals/runs/<arm>/` with
the prediction, dispatched inputs, published SHAs and run id.

`run_timeout_hours` is enforced by CI, not by you: a run that reaches it is killed and returns
nothing. Use it as a planning bound — do not dispatch what the top of its estimate exceeds. Split
suites on task boundaries so parts stay scorable, and give a multi-eval run per-eval timeouts
summing under the cap.

Infrastructure failures (runner offline, image pull, unrelated timeout) are re-dispatched, not
scored. Crashes, loops and malformed output are measurements. This holds for the release run too:
its coordinator must allow a re-dispatch after an infrastructure failure, and the release route
(the repository, workflow and token that pull the image) is proven in the survey's preflight, not
first used at the end.
