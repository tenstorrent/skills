---
"tt-project": patch
---

Path guard: a script that only reads a `guards.protected_paths` folder, and a command that mentions
one only in quoted data (a JSON hand-off, a note, another command's argument), now pass. Python code
is parsed and each write call is checked by its target; a call not known to only read fails closed,
and nothing the checked code defines is trusted. The default credential folders get no such relief
and keep the old whole-line check, and so does any command that changes its working directory.
Every write form is still refused.
