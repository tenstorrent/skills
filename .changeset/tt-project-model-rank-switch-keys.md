---
"tt-project": patch
---

`tt-project`: model ranking fixes. With the model pick on, a model-down switch is kept per tier and picked model, so a pick that routes one tier to an up and a down model no longer logs a false "back" and alerts again on each start; tier and routes-rule switches keep their own keys. A preference-pinned `chat` task's ranked stand-in that is refused is now skipped for a day, as a routed one is.
