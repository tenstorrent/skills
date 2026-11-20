---
"tt-project": patch
---

`tt-project`: the credential tripwire (credwatch) and the shell guards read a command holding a huge word in time linear in its length. Python's shlex grows a word one character at a time, so a 2 MB word took minutes, stalling the daemon's thread as runs ended. Long runs of plain characters now reach shlex as a short mark and are restored afterwards (hook._words), the quote and comment scan copies such runs in one step, and a call holding neither the file's name nor its writer's command word is passed over unparsed. Verdicts are unchanged.
