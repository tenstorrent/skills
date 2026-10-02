---
"tt-project": patch
---

`tt-project`: a resource that several of the user's projects on one machine use can be declared
shared (`shared_resources` in the config, or `ttp machines add <alias> --shared [names]`). Its lock
slots, reservation and pause then live under `~/.tt-project/locks/<resource>/`, so `ttp lock` and
exclusive tasks of every project take turns on the same slots and a pause holds in all of them;
holders and pauses name their project. Where projects give different slot counts, all use the
smallest, and `ttp status` and the coordinator digest say so. A pause outlives the resource leaving
the share: `ttp machines` refuses to unshare or remove a paused one, and a project that drops it
from its config keeps the pause as its own.
