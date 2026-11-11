---
"tt-project": patch
---

`tt-project`: an open run row, whether left over from an earlier boot, with a dead pid or live, no longer changes a credential-watch verdict. Before, any open row on the host marked a credential change as shared, so a stale row could excuse a run whose own transcript wrote a credential store. Now a run's own transcript alone decides whether it fails, and a change made outside every run (including a permissions-only change) is recorded and told once for the host without failing any run.
