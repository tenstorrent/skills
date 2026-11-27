---
"tt-project": patch
---

The GLM guard no longer reads an escaped backquote inside a quoted `python3 -c` string as a hidden command, so a check whose text mentions a reset or power cycle is not taken for a device step (a real backquote substitution, and an escaped one handed to any other program, still is), and `ttp remove --help` is a usage read that passes while `ttp remove <project>` stays refused.
