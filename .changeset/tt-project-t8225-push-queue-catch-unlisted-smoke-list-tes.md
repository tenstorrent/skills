---
"tt-project": patch
---

`tt-project`: a push batch runs delivery.push_entry_checks on each entry merged onto its head and sends back only an entry that fails one (a test file missing from the smoke list), so it never fails the batch.
