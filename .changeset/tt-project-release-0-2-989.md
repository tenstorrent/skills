---
"tt-project": patch
---

tt-project 0.2.989: one release of everything landed since 0.2.987, with its full suite run once. Per-call cost booking (item 1: 3758667cf..5f99527df). Model failover with a per-source untrusted label (item 2 part 2: 70264110e) and the shared daily model bench with its weekly recheck (item 2 part 3: 95bdc268d, 27e20ede3, 87ed5c645). The full Claude Code harness, one push train with a smoke gate, the overseer, config provenance and keep-awake off by default (items 3, 4 part 1, 5, 6 and 8: 3bc14129b), the full-harness coordinator and coalesced coordinator wakes (items 3 parts 2-3 and 5 part 2: 20c3b8185). Post-landing checks that bisect a failure to its change and revert only that change (item 4 part 2: 22b2bd10d, a435e691b). The run-time secret store, so shared hosts keep no plaintext keys (item 7: 10db0691c, c42c6f39f, 260d3620a). Each change's own changeset gives the details.
