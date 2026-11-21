---
"tt-project": patch
---

Config breakage watch: when two changes of one key share a millisecond timestamp, blame the newest
(the last log row), so the event's wording never mixes in the older change's undo lines.
