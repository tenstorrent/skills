---
"tt-project": patch
---

`tt-project`: 0.2.28 handles host sleep and long ticks. Run timeouts and the stall guard count
only awake time; a run a host sleep cut is requeued without spending an attempt (up to
`budget.max_reboot_losses`) and is not counted as waste. After a wake, nothing new starts for
`budget.wake_settle_s` (default 300 s), though messages from people are still answered. A run
with no output and no tokens costs $0, and only successful runs can say a plan stopped reporting
its windows. A long tick now pings both watchdogs between its steps, command watcher timeouts are
capped below the watchdog, and open asks always lead the top section, whatever their age or number.
