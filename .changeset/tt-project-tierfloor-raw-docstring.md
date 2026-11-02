---
"tt-project": patch
---

`tt-project`:

- tierfloor.py: the module docstring is a raw string (r"""..."""), so its `\d` is no longer an invalid escape sequence (DeprecationWarning today, SyntaxWarning on Python 3.12+); the docstring reads the same.
