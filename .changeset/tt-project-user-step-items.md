---
"tt-project": patch
---

`tt-project`: a step only the user can take (a sign-in, a device code, a credential) reaches the user once per task, not once per run. A worker names it with `ttp notify --step <kind>`, plus `--check` (a model-free command that exits 0 once the step is done), `--do` (what the user runs) and `--expires-in` (minutes until the link or code lapses). In a run that hands off waiting or blocked, a notify whose text names such a step counts as one too. The first notify opens one "needs you" item; later notifies from any run of the task update that item in place, a new link or code included, and post nothing new. A waiting hand-off that gives no retry_when, or only prose such as "the user's next message", waits on the step's check instead; with no check, the coordinator is told and the prose is dropped. When the check passes, the item clears with one quiet line and the task wakes. A task that ends clears its items.
