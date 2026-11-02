---
"tt-project": patch
---

`tt-project`: config setter records read against project.json as it is now. A key never set reads as `default (never set)`, not unknown. The load-caps upgrade, the home-zone follow (`ttp connect`, a spend push) and `ttp throughput --undo` record a default or harness setter instead of none. Each record keeps a digest of the value it wrote, so a hand edit of project.json after a recorded set reads as unknown, never as the old setter; `ttp config get --why` and the self-block setter check both use this.
