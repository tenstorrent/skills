---
"tt-project": patch
---

`tt-project`: the guide now says that a `ttp checks` pass that `ttp push --own` reuses ran with
`TTP_PUSH_BRANCH` empty, so a check that depends on `TTP_PUSH_BRANCH` (like one that depends on
`TTP_PUSH_TIP` or on mode `own`) cannot count on running in that pass.
