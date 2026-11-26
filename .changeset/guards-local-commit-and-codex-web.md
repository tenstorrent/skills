---
"tt-project": patch
---

`tt-project`: a project.json, charter or memory change that a harness task commits while a full-tools coordinator's shell call runs is no longer put back by the coordinator's protected-file guard (a task run's `git add`, `rm`, `mv` or commit in a harness checkout logs the protected files it staged or committed, as ttp's own writes are logged, so an edit staged in one coordinator call and committed in a later one is kept; an edit not yet staged when a call ends is still put back, its content saved). Codex's web search runs on the model's server, where no hook sees it, so a project whose `guards.local` guards define `tool_refusal` (or star-import a module, or do not read) now runs Codex with web search off (`web_search="disabled"`), and the provider matrix says so.
