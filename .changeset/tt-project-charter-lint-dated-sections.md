---
"tt-project": patch
---

`tt-project`: The daemon now checks the whole charter whenever it changes and flags a dated section
(heading with a date, such as `(user, <date>)`) that allows or narrows what an earlier Restrictions item
forbids while that item still stands. Each such pair raises one `charter_conflict` event, once ever. The
check uses no model and never edits the charter. Before, only conflicts added by a coordinator charter
update in a user turn were caught, so sections edited in by hand could silently lose to the old item.
