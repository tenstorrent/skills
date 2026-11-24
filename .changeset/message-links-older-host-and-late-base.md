---
"tt-project": patch
---

A laptop upgraded before its project host no longer loses its chat listener: an older runtime on
the project host that rejects a newer optional flag (`--holder`, `--link-base`, `--link-as`) gets
the command again without the flags it named as unrecognized, one retry per flagged option, so the
listener relays its messages instead of exiting on the parse error. A link base whose check failed
(a tunnel or web app still coming up when the listener started) is decided again while it stays
unset, at most once a minute, so messages from later batches and reconnects carry the checked link
without restarting the listener; the forwarded-base proof row is recorded once per change of the
base, not once per batch.
