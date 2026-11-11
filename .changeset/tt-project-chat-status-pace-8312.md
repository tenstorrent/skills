---
"tt-project": patch
---

Chat status pace. A status post (progress, counts, ETAs) goes to a chat at most once per `coordinator.chat_status_gap_h` hours (default 1, per chat in `coordinator.chat_status_gap_chats`); a sooner one waits for the chat's digest, and the hourly status table keeps the same gap. Answers, asks, outages and results go at once. A post that nearly repeats what another chat of the same user got in the last hour is not sent again, and that chat's promise on the same topic is recorded as delivered by the first post. The coordinator digest shows a chat's own status interval, and `ttp stats --chat` counts repeats not sent.
