---
"tt-project": patch
---

Push batches follow `delivery.push_branch`. A new batch takes its target from the setting, and a resumed or later round re-reads it. A batch whose target no longer matches stops before more checks or a push and hands its entries back approved, with one event. Retargeting keeps an unchanged head's review approval when the new target's tip contains or equals the old one; otherwise the review goes back. Entries already on the target land as `landed`. After three failed batches on an unreachable remote, one alert names the setting and the remote, and no batch starts until a bounded probe reaches it again.
