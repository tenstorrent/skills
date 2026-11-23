---
"tt-project": patch
---

Screen: an open keyed watcher issue now follows a lower severity hint down (ignored at info, with a history entry), so a watcher fixed to report a condition at info stops re-waking the coordinator. A hint back up within the reraise window restores it quietly (a flap); a recurrence after that raises and wakes. Grace repeats now carry the line's own rating (at least normal), not low, so a still-failing check keeps its issue up and reminds by the rewake rule. Unkeyed issues, reopening a fixed issue and raising stay as before.
