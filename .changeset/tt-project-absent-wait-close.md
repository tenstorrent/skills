---
"tt-project": patch
---

`tt-project`: a wait on an outside listing or resource already shown absent is decided now, not held to its deadline. When a waiting task's retry_when or a queued task's start_when queries something outside (curl, a URL, `gh api`, a package index) and gives the same absent answer (HTTP 404, not found, not listed) `waiting.absent_close_n` times in a row (default 3, 0: off), or its hand-off already cites settled evidence (repeated 404s, "not listed"), the daemon's progress check closes it model-free as done with outcome `unavailable` and registers one reopen watch: the same probe every 6 h for 14 days, whose first pass queues a `reopens:<id>` follow-up. Credential, funds and quota-reset waits (budget, spend, rate limit, a daily reset, login), time-only and own-job waits, and code and review tasks are exempt.
