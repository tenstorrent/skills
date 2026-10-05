---
"tt-project": patch
---

A user's yes to a charter change stays valid until the change is applied. When a charter_update from the user's own turn is rejected (say an ambiguous `replaces`), the daemon records it in project.db (messages, ask, section, target, text and its hash). A later turn without a user message may apply the same change once, text matching up to whitespace, within `coordinator.charter_approval_days` (default 7); a refused retry says whether no approval is on record, the text differs, it was used or it expired. It is not a PR approval. `replaces` takes a section's full heading or number; a prefix matching several is rejected with just those candidates, and the coordinator digest lists the exact headings on one line. A user's change or lifting of a restriction also merges the permanent Restrictions sections into one block (temporary ones keep their own section and end), with the merge in CHARTER.history.md.
