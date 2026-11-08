---
"tt-project": patch
---

`tt-project`: a logged-out gh no longer fails quietly. One model-free check per machine, shared by every project on it, runs `gh auth status` at most every 15 minutes while a project there uses gh. While it fails for the login, one normal alert names the machine's ssh alias and the user's step (`gh auth login`), is sent once per outage (the other projects show it quietly) and clears once the check passes. pr-watch now ends with an `error:` status rather than ok when its gh calls fail for the login. `ttp ci` exits 4 with a `broken:` line rather than 75 ("not yet"). A `start_when` or `retry_when` probe that fails for gh's login is folded into the host alert: a waiting task sleeps on (its status says it waits on the machine's gh login) rather than taking a model wake or a broken-probe event per task, and wakes as broken only once gh is logged in again.
