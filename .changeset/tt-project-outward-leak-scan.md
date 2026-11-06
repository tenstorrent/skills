---
"tt-project": patch
---

`tt-project`: a run's outward writes are leak-scanned before they leave: `gh` calls through the harness's `bin/gh` (arguments, body files and stdin), `ttp note --to` and `ttp notify` are refused when they carry a stored secret value, a common credential shape or a project forbidden-data pattern, with one line naming the class, never the text. Chat posts mask credential shapes.
