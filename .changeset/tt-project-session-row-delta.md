---
"tt-project": patch
---

`tt-project`: a resumed persistent coordinator session wake sends only the Open tasks section row by row: a changed task row is sent whole and named by its task id as added, changed or removed; never a partial row or a text diff. Every other section (memory, user corrections, promises, chats and the rest) is compared as raw text and sent whole on any change, so an entry edited only in a number and unit (such as '30 min' to '2h') now reaches the session. Open tasks still goes whole when a task id repeats, its list is cut short by its row limit (now too), its header changed in more than its counts, more than half its rows changed, its rows changed order, or on the session's first wake after it starts, resumes or is compacted. The new setting `coordinator.session.row_delta` (default on) turns per-row updates off. The stateless coordinator's digest is unchanged.
