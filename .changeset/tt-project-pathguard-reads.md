---
"tt-project": patch
---

Path guard: a script that only reads a `guards.protected_paths` folder, and a command that mentions
one only in quoted data (a JSON hand-off, a note, another command's argument), now pass. Python code
is parsed and each write call is checked by its target. Other code, and Python too dynamic to follow,
keeps the old check. Every write form is still refused.
