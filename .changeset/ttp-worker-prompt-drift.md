---
"tt-project": patch
---

`tt-project`: a project's own `harness/prompts/worker.md` that grows much larger than the template's (over `coordinator.prompt_drift_ratio`, default 1.5x; 0: off), lacks a template section, or keeps a section the template has dropped is now flagged without a model call. `ttp doctor` and the daily review show one line, and the daemon queues one low `worker_prompt_drift` event to the project's own coordinator per changed copy, suggesting it merge the copy toward the template. It is not repeated while the copy is unchanged. The template compared is the project's last upgrade snapshot (its harness's `upstream` branch); the harness never edits a project's prompts.
