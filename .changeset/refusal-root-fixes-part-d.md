---
"tt-project": patch
---

`tt-project`: fewer coordinator actions are refused when their meaning is clear and harmless.

- A memory rule that quotes only a relayer's context is stored as relayer data, not as the user's words. A charter line that does the same is still refused.
- A reply that repeats a cited run sends only its new part.
- A claim state that differs only in case is read as the allowed state.
- A receipt on partial or blocked owner work is kept as progress, and the obligation stays owed.
- A missed due moved with no plan change is applied, and its chat is told the promise was missed and moved.
- A self_change sent without an undo gets the undo its kind gives, where the kind alone gives one.
- A scoped exception whose `excepts` clause spans sentences of one ban matches that ban.

Other fixes:

- `ttp config --account` inside a run refuses every key the one needs-the-user's-word list names.
- A bare config key maps onto its full key only when exactly one ordinary section holds it.
- The landed check caches its patch-id reads per revert and tip.
- A local `ttp checks` rerun of a check that sets PYTHONPATH itself still loads the select plugin.
- The GLM quota view drops a key nothing reads.
