---
"tt-project": patch
---

Spill quarantine no longer reads unrelated failures as a spill config fault: a failed check's signature now names its failing pytest ids, and a failure with no error line and no test id (`exit N`) never matches another host's, so it counts only as a strike and a host failing that way is still quarantined.
