---
"tt-project": patch
---

`tt-project`: removes the ended effort A/B split (Jev's effort pick stays, off by default) and the coordinator digest section "Guards behind the charter", and folds small modules into their neighbours with no change in behaviour: etahist into eta, pagesev into alerts, killscan into svcscan, installcheck into release, express into pushq and benchowner into modelbench. The commands `ttp express`, `ttp install-check`, `ttp killscan`, `ttp guards`, `ttp course`, `ttp model-trip` and `ttp chat-load` work as before. `ttp config --account KEY --add V` is now a usage error instead of being dropped silently.
