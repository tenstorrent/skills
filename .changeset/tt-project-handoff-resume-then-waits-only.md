---
"tt-project": patch
---

`tt-project`: on a provider with no Stop hook (Codex), a hand-off with a valid status that Claude's Stop hook refuses (a bad `then` list, or a `done` whose `continue:` follow-up only waits) is no longer taken as is: the daemon resumes the same session once, told the Stop hook's own problem text, to fix it. A second such hand-off is taken as before. Claude runs are unchanged.
