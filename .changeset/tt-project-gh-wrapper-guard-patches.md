---
"tt-project": patch
---

`tt-project`: the guard that keeps runs outside a harness task from editing the harness's gh wrapper and its pre-hooks now also reads a patch inside a shell command (`apply_patch <<EOF ...`): each add, update, delete and move target is checked as a native Codex `apply_patch` is, relative paths included, so the same edit is refused on every provider.
