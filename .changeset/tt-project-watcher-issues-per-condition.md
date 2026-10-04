---
"tt-project": patch
---

`tt-project`: command-watcher issues are kept one per condition. A line written as
`<subject>: <item>; <item>` gives one issue per item, keyed by source, subject and the item's kind, with
counts and changing numbers masked (digits inside identifiers are kept). Items may start with `now`,
`still:` or `changed:`, and `cleared: <item>` closes the issue without waking. A watcher run that prints
nothing closes that watcher's open issues, and watcher issues not seen for 24 h close; closes never wake
or alert. A closed issue seen again reopens and wakes only at or above `screen.wake_min_severity`, and an
issue kept quiet at info that the watcher later rates higher wakes once. Issues record when and why they
closed; a one-off migration adds the columns and closes the open per-text watcher issues left by the old
keys.
