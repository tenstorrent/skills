---
"tt-project": patch
---

`tt-project`: a coordinator action with a malformed field (a task id that is not a number, a priority that is not a whole number, a spec sent as a list, a reply with no text, a `type` that is not text) is refused with a plain reason in apply and in the in-turn dry run, never with an internal error; an id written `#12` applies as meant. The dry run reads a task's `start_after` in the home zone as apply does, so it refuses exactly what apply refuses. `ttp config --chat-settable` reports as accepted the keys a full-tools coordinator's `config_set` applies as `ttp config` does; `budget.*`, the push grants and `machines.allowed` stay the user's own. Clear-meaning rewrites are tallied once per applied batch, so a batch applied again after a cut-off is not counted twice.
