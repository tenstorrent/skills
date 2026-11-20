---
"tt-project": patch
---

`tt-project`: only a harness task may change a file listed in a project's `guards.local`, now also when the edit is a Codex `apply_patch`: a patch that adds, updates, deletes or moves to a listed guard file is refused in other runs, as Edit, Write and shell commands naming it already were.
