---
"tt-project": patch
---

`tt-project`: a resumed persistent coordinator session wake now sends per-row updates for every list section with stable row keys, not only Open tasks: promises, memory and correction entries and attached chats too. A changed row is sent whole and named by its key as added, changed or removed; never a partial row or a text diff. A section goes whole, as before, when it has no row keys, a key repeats, its list is cut short by its row limit, its header changed in more than its counts, more than half its rows changed, its rows changed order, or the session's first wake after it starts, resumes or is compacted. Open tasks past its row limit now goes whole instead of row by row. The new setting `coordinator.session.row_delta` (default on) turns per-row updates off. The stateless coordinator's digest is unchanged.
