---
"tt-project": patch
---

Fewer repeated test runs. A push batch with one change reuses that task's own full `ttp checks` pass when the patch is the same and newer upstream commits touch no path the check covers. It judges the change without the batch's own version bump. A targeted (narrowed) run never stands in for a full check. `ttp checks -- <extra>` drops an extra plain pytest run of paths that a push check's pytest path already covers, and prints a one-line notice. It still runs that extra when the push check runs only the tests the diff reaches (targeted) or the fast path skips it.
