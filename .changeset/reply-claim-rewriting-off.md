---
"tt-project": patch
---

Coordinator replies and notifies go out exactly as written: the reply-claim check that
rewrote 'landed'/'live' lines is off (a 'landed'/'live' line is never
marked, rewritten or negated on the send path). pushclaim's reply checker stays in the code, unused, until the tag-only
version lands.
