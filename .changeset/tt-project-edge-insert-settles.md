---
"tt-project": patch
---

`tt-project`: the push batch now settles a conflict where one side added a block right beside a line the other side only reworded or deleted. Both sides' lines land as written, the batch's checks still run on the result, and the change keeps its approval instead of going back for a conflict fix. Insertions inside an edit, at a spot both sides insert at, or whose placement the line diff cannot tell stay conflicts. The daemon's tick step list is now one step per line, so two changes that each add a step merge as lines both sides added. Over the last week's push queue records these two patterns account for about 12 of 140 approvals sent back.
