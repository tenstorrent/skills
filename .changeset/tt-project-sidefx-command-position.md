---
"tt-project": patch
---

`tt-project`: the outside-writes scan treats grep, rg, sed, awk, ssh and sh as tools only in command position (a command's first word, past VAR=val, sudo, env, xargs, time and the like). A path argument ending in /rg, /sed or /ssh no longer hides the write targets after it, so `cp ./tools/rg ~/.ssh/authorized_keys` names its target again.
