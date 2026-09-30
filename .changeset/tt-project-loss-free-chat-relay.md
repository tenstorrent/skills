---
"tt-project": patch
---

`tt-project`: the chat relay no longer loses replies.

- The listener reads the newest message id before it queries, so a reply posted between its reads
  is shown instead of being marked read unseen.
- `ttp listen --ack <id>` marks messages read only once the chat has shown them. A listener that
  printed to a closed chat or dropped connection hands the same messages to the next listen.
  Printed lines carry the message id so repeats can be skipped. Without `--ack` the old behavior
  stays.
- The web app's Chat tab shows messages from every chat, labelled with the chat.
- A config edit made within one filesystem clock tick of the previous write is now kept as the
  last good copy.
