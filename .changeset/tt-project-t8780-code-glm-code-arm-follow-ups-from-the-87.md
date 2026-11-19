---
"tt-project": patch
---

`tt-project`:

- code arm: a model-free side-effect guard on GLM-arm runs (ttp, hook, runner stop), a refused attempt ends the run as a bounce to Opus
- code arm: live mini-swe-agent harness for the GLM arm, the guard in its command layer, bridge-costed with cached tokens at the cache-read rate
- code arm: guard and harness writes follow the durable-write rule; no project name in the tests
- code arm: policy files stay on Opus (classified by spec before the coin, a GLM diff that touches the list is refused and handed back), a project with no list cannot run the arm live
- code arm: an optional digest of large tool output for the mini-swe-agent GLM harness (failing lines plus a tail, the whole output saved), context tokens per call in the run's result line
- ttp remove: take a project off the machine in one code path (stop, service files, shared per-machine rows pruned, unregister; files only with --delete-files)
- deploy check: follows only projects registered on this machine; a scratch or removed project never raises a deploy alert and its open one closes
- review fix: continues the task the review depends on, never one its spec only mentions
- docs and changeset: the code arm's guarded harness, policy files, digest, ttp remove, deploy check and review-fix lineage
- smoke list: the ttp remove case file's fastest tests
- test: a claude-code model-lookup line on stderr is not a refused model when the run worked
- policyfiles: git through gitmemo.run (the git-helper guard); remove test registers its second project without a second clone
- tests: the live arm tests use the guarded harness and a policy list; remove and deploy-check tests fit one project per repo; glmenv exempt from the stdlib-only rule
