---
"tt-project": patch
---

`tt-project`: `ttp push --detach` starts promptly even when the remote stalls. Its base-branch check
now fetches the push target with the same bound as the reach check (no prompt, no stdin, at most 60 s)
and skips the check when that fetch times out, as it does when the fetch fails.
