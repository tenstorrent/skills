---
"tt-project": patch
---

`tt-project`: on macOS the access bridge keeps its workstation awake on charger power only, never the display. While a bridge session runs, its relay holds `/usr/bin/caffeinate -s -w <relay pid>` (system sleep prevented only on AC power; never `-d`, `-i`, `-u` or `-m`), which ends with the session, so a restart never leaves two; nothing is held on battery and other platforms spawn nothing. New top-level bridge setting `"keep_awake"` (`"on"` by default, `"off"` turns it off); `ttp bridge status` ends with one line saying held, off by setting or not on macOS. `ttp setup` and upgrades restart a session whose command changed, so an installed bridge picks the hold up without a reinstall. The daemon's own keep-awake already holds only on AC power and never the display; a test pins that. The README and guide state the rule. Tests use a fake platform and a fake process start only, never the real `caffeinate`, `launchctl` or `pmset`.
