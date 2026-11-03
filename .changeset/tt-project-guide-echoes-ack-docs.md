---
"tt-project": patch
---

`tt-project`:

- Document the new `ttp echoes --ack <retirement> <place>` flag in the plugin guide: the acknowledgement is stored in the project's kv store (`retired_echo_acks`) with a hash of the matched line; while that line is unchanged the match is listed as `acknowledged` and gets no fix order, and a changed line or a new copy lists the match again.
