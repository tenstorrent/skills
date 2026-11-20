---
"tt-project": patch
---

Urgent messages never go silent. A chat message or `ttp say` is urgent when its sender marks it (`urgent:` prefix, `ttp say --urgent`, `ttp note --to ... --urgent`) or a conservative model-free classifier sees an urgent marker or a report that a person's live service is broken (`urgent_msg` settings). The daemon acks it within 60 s in the sender's chat, naming who is on it and when the first finding is due, and keeps the deadlines on disk. A missed first-finding deadline tells the sender's chat the reply is late with the next check time, wakes the coordinator with a must-act item (or hands it to the running turn) and, a second miss later, notifies the user at high. The coordinator posts its first finding mid-turn with `ttp urgent-reply`.
