---
"tt-model-bringup": patch
---

Find serving gaps in the full-model stage: pin the serving plugin and vLLM pair and record the
plugin interface, capability, and request-feature contract in `serving_contract.json`, which
the stage 6 and stage 9 gates check. Add
plugin change rules: the model owns model gaps, plugin defects need a justification record
and a regression test, and new model capabilities, plugin environment variables, and
`--additional-config` options need human authorization. Remove skill text that invited plugin
edits, accept an authorization-pending plugin change as a goal blocker, and make stage review
check plugin checkout changes and declared capability keys.
