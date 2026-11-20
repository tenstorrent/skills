---
"tt-project": patch
---

`tt-project`: a pause the user set never ends by itself, and status now says so. `ttp status`, the web app and the coordinator's digest show it as set by the user, ending only on the user's word, and once its `end_when` or `until` passed, as "end condition passed at <time>; lifting needs the user's word". The one observation the coordinator gets when it passes says the pause stays, to tell the user once, never to say it is lifting, and to lift it only in a turn that carries the user's word. The coordinator prompt says the same. A broken `end_when` probe, or a failed job it waits on, is never shown as passed: the coordinator is told once that the end condition can no longer be checked. A machine-wide user hold whose end condition passed now shows it in `ttp status` too.
