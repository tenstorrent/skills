---
"tt-project": patch
---

Links: an id the linker cannot resolve now stays exactly as written (no "(unresolved reference)" suffix). Command lines, fenced blocks, code spans and a `#N` inside a longer token (a branch name, path or URL fragment) are never rewritten, and a `#N` right after a repository name or `PR`/`pull`/`issue` is left as a GitHub ref instead of linking to a task page.
