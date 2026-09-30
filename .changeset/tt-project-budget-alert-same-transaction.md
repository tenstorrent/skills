---
"tt-project": patch
---

`tt-project`: budget gates and their alerts are saved in one transaction, so a crash or a locked
database between the two no longer loses the alert for good.
