---
"tt-project": patch
---

`tt-project`: `ttp machines change add|edit` takes `--until-probe` with `--expires` (the entry ends at whichever comes first); giving an entry its until-probe again, even the same one, drops its cached pass, pages and pass streak, and a probe run begun before that does not count.
