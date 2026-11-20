---
"tt-project": patch
---

Push and urgent checks runs now count toward the host checks cap (`checks.host_max`) and draw on the test pool (`checks.test_pool`), still ahead of every waiter. The status line counts every run (`host checks: N running of cap M (incl. push P, urgent U)`). The new account option `checks.priority_beyond_cap` (default off) restores the old overflow beyond the cap and pool.
