# Data kept out of git

The config declares it, never charter prose: `memory.forbidden_patterns` (regexes or `{name, regex}`) and `memory.forbidden_patterns_file` (one regex per line, relative to the project folder; keep it under `state/` when the patterns would reveal the data, e.g. names). A coordinator `charter_update` to a `Forbidden data` section adds its backticked spans (the words before one on a bullet name it) to `memory.forbidden_patterns`; an older charter's section moved there once. Editing the charter's text by hand changes no pattern.
`ttp memory`, the coordinator's `memory_add`, `memory_forget` `why` and `charter_update` refuse matching text before writing or committing it; the error names the pattern, never the text.
Declaring any also gives the coordinator its rule to store pointers to work files, not figures.
None are set by default; an unreadable file or invalid regex refuses every write until fixed.
