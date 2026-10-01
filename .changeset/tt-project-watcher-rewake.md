---
"tt-project": patch
---

`tt-project`: Command watchers can now wake the coordinator again for a known open issue. An
observation with `"repeat": true` wakes on every occurrence, and a known issue that comes back after
more than `screen.rewake_after_h` hours quiet (default 6, per-watcher `rewake_after_h` in the
schedule payload, `null` turns it off) wakes again. Log watchers are unchanged.
