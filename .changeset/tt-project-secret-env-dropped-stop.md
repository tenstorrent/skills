---
"tt-project": patch
---

`tt-project`: a sandboxed `ttp secret env` stopped by Ctrl-C or SIGTERM withdraws its queued request at once even when the stop lands while a Python finalizer runs (which drops the exception it raises): the stop is recorded by its own handler and acted on at the next poll, instead of the command waiting out its 90 s start window.
