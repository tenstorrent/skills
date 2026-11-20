---
"tt-project": patch
---

Push batch checks judge a failing check as `ttp checks` does, reusing its judge (gatejudge). A failing test that also fails on the tip alone on the same host (pre-existing), passes on its rerun (flaky, then quarantined) or is quarantined and untouched by the batch no longer fails the tip or blocks the batch: the check passes and the test is reported. Every failing test not yet judged at a head is rerun there (before, one test rerun anywhere in the batch stopped every later rerun). The split search probes each prefix with only the new failing tests and runs the full suite once, at the prefix they pass on, instead of at every prefix.
