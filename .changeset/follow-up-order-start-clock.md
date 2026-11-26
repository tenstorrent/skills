---
"tt-project": patch
---

A follow-up order on a message that already has an order starts its clock at its own record, not at the message: no false `order_late` alarm, and the late-start count no longer lets a later order replace the first.
