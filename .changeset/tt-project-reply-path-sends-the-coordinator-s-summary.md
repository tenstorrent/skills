---
"tt-project": patch
---

A finished reply-chat task's hand-off never posts to the user's chat: the daemon raises one
`reply_due` event for the coordinator, naming the chat and the landing facts with the hand-off
enclosed as data, and the coordinator replies in plain words. When no reply reaches the chat
within `coordinator.reply_fallback_s` (default 20 min), the daemon posts one short model-free
outcome line built from records (task, landed commit and version, any user step). Each reply's `delivers` is recorded per
message, so a reply about one task never disarms another task's fallback in the same chat.
