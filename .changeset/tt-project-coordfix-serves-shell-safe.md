---
"tt-project": patch
---

`tt-project`: the `ttp audit why --serves` examples and error text in the coordinator's hands-on fix
recorder now show the shell-safe forms (`msg1776`, `task6302`) instead of `#`-prefixed ones: an
unquoted `#` starts a comment in bash and would drop the argument. The parser accepts both forms.
