---
"tt-model-bringup": minor
---

Pin every moving part of the bringup stack at startup and refuse stale ones. The new
`scripts/check_stack_freshness.py` resolves, for a new run, the tt-metal checkout against its
upstream default branch, the TT vLLM plugin checkout against its default branch, the installed
`transformers` against the checkpoint's `transformers_version`, and this plugin's installed
version against the published one, then records them in `doc/stack_pins.json`. A checkout more
than seven days behind upstream, a `transformers` below the checkpoint minimum, or an outdated
skills plugin is critical for a new run and advisory on `--resume`. A new stage 1 gate verifies
the record exists and the checkout still descends from its pin; the stage 9 gate also verifies
the serving contract's plugin commit equals the pin. The model-bringup skill documents the rule
and `serving-stack.md` moves the plugin pin to startup.
