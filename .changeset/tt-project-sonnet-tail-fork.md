---
"tt-project": patch
---

A worker run on another Claude model than the coordinator's (a Sonnet run of an Opus coordinator) now forks the coordinator session instead of starting from the disk block. The fork keeps only the session's last turns (`coordinator.session.worker_fork_other_model_tokens`, default 8000, 0 turns it off). Its start moves only in steps, and it sends the coordinator's tools and system prompt on the run's own model, so all such runs share one small cache entry. Models outside the Claude families and providers that cannot copy a session's last turns still take the disk block. The hourly fork rate now reports forks, resumes and disk-block starts separately and no longer counts disk-block starts as forks.
