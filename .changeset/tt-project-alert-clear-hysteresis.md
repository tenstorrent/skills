---
"tt-project": patch
---

`tt-project`: a fault that fails and recovers over and over is one alert and one clear, not one of each per recovery. An alert episode with a clear message (`Cleared: ...`, such as "Runs cannot start") now closes only once its condition has stayed healthy for 10 minutes (`alerts.CLEAR_HOLD_S`): a repeat report or a failing check in between restarts the wait, and the repeat only updates the open episode's last-seen time, as before. A fault that recurs after its alert cleared alerts again. Explicit closes (acknowledging an alert, a project resuming) still clear at once.
