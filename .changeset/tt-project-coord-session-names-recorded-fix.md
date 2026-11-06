---
"tt-project": patch
---

`tt-project`: a full-tools coordinator session's Tools section now says that each fix it makes itself is recorded with what (the logged calls), why, the order or task it serves and its undo, names `ttp audit why "<why>" --serves <o12|msg123|456> --undo "<how>"` and `ttp audit fixes`, and no longer repeats the heavy-work rule from Managing. Sessions without tools (read-only or none) and the stateless coordinator prompt do not name it: they cannot run `ttp audit why`. The guide's coordinator overview points to `ttp audit fixes`.
