---
"tt-project": patch
---

A chat message's id never becomes a task link: `Re #N`, `msg #N`, `message #N` and `user msg #N` stay
plain text, and a bare `#N` links as a task only when no chat message has that id (task and message
ids overlap). `task #N`, `run #N` and `review #N` link as before, and a list such as
`tasks #N, #M and #K` links every id in it. The coordinator writes tasks as `task #N` and names a user
message by its time or wording.
