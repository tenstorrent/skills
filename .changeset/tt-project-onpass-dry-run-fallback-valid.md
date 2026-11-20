---
"tt-project": patch
---

`tt-project`: the `on_pass_cmd` dry run counts a `ttp checks --result` wait's `on_pass` as the fallback for an exit 0 without `$TTP_RESULT` only when the hand-off carries an `on_pass` that would be recorded (done or needs_review with a summary, no wait), as the probe-pass path does. A hand-off with no or an invalid `on_pass` is now handed back instead of waking a model later.
