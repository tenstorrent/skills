---
"tt-project": patch
---

tt-project 0.2.991: forked worker and reviewer runs read the coordinator's prompt cache instead of writing it again. A fork copies the coordinator's session, but it was sent with a different tool list (no StructuredOutput tool, other setting sources, other skill listing and Bash timeouts) and a different system prompt, so the API reported `tools_changed` and wrote the whole copied history again (a first call wrote about 43k tokens and read none). Now a fork on the coordinator's model sends the coordinator's tools, settings, schema and system prompt, and its own rules lead its first message; a fork that cannot match (another model, its own schema or tool limits) keeps its own prompt and records why in the run note (`ctx_fork.prefix`). The coordinator gains the workers' setting sources and lean settings, so its cache is written once more after the upgrade.
