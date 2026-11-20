---
"tt-project": patch
---

`tt-project`: a wait probe that starts with plain `echo`, `printf`, `true` or `:` statements joined by `;` (for example `echo "waiting on: ..."; test -e state/user-words/user-x && <check> || exit 1`) is now seen as gated on its user marker, as the same probe without the echo already was: while the marker is missing the progress check raises no probe stall and the waiting check raises no `wait_stale` for it. A leading term with a command substitution, backticks, a redirect, a pipe or any other command still leaves the probe a live one.
