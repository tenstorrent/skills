---
"tt-project": patch
---

An `after_push` that exits 75 (a box it must keep off is busy, such as a live stream) is now `deploy deferred` at normal severity, not a high failure: one event per deferral, no alert, and the daemon retries it by itself every `delivery.after_push_retry_s` (default 300 s) until it deploys. A newer push replaces a pending retry, the retry survives a daemon restart, success ends it with one low event, and a deferral open past `delivery.after_push_defer_alert_h` (default 24) tells the coordinator once (user msg #3557).
