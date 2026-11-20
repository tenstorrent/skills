---
"tt-project": patch
---

`ttp ack-wait --terminal` reads a reply sent as `[k/N]` parts as one answer once every part is in. A finished task in the sender's records backs it, so evidence words such as "started" in one part no longer hold the wait. Acknowledgements and plans, single or multi-part, still wait. A later part joins its packet by the sender's task even when it does not name the note. While parts are missing, the probe says how many are in.
