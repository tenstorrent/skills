---
"tt-project": patch
---

tt-project 0.2.990: the runtime loads on Python 3.9 again. hook.py named the coordinator's hook event with `:=` inside a subscript, which is a SyntaxError before Python 3.10, so a project whose service runs a 3.9 interpreter (such as macOS Command Line Tools' python3) could not start its daemon after an upgrade. A new test finds unparenthesized `:=` that 3.9 rejects (as a subscript index or a set element), which `ast.parse(feature_version=(3, 9))` lets through on a newer Python, and, where a Python 3.9 is installed, compiles every runtime file and imports every ttp module under it.
