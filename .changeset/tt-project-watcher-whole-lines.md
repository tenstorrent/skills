---
"tt-project": patch
---

`tt-project`: command watcher JSON lines split at `; ` again by default, as plain lines do: each item is
one issue and `cleared: <item>` closes it. A line that must stay one observation now says `"whole": true`
(one issue for all the text after its subject, one mute condition and one receipt subject, a long title
truncated); the `"items"` flag is gone. A `"key"` or a multi-line observation still stays one issue. Info
lines (`"severity": "info"`) take the usual path again, so their `cleared:` items close issues and they
replace older receipts of the same subject; they still reach the machine ledger. Known gap: info lines are
not suppressed, so an item first seen at info is recorded as an issue that does not wake.
