---
"tt-project": patch
---

Status digests, the hourly status table and the coordinator's cost-per-wake alarm stay in the web app by
default and no longer go to chats; the alarm still reaches chats when it needs the user. Set
`notify.chat_status` or `notify.chat_cost_alarm` to `true` to send them to chats again. The guide's
hourly table line now says test runs are CPU work with no model spend.
