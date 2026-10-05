---
"tt-project": patch
---

`tt-project`: The daemon now checks the whole charter whenever it changes and flags a dated section
(heading with a date, such as `(user, <date>)`) that allows or narrows what an earlier Restrictions item
forbids while that item still stands. It leaves alone a sentence that keeps the item's own limit
("jobs may queue through each broker" against "jobs only through each broker"), Resources sections, items
that limit nothing, and items dated after the section, since file order is not time order. An exception
worded with "only" ("now allowed only for hotfixes") still counts as a conflict. Each such pair raises
one `charter_conflict` event, once ever. The
check uses no model and never edits the charter. Before, only conflicts added by a coordinator charter
update in a user turn were caught, so sections edited in by hand could silently lose to the old item.
