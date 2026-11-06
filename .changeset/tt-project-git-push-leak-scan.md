---
"tt-project": patch
---

`tt-project`: every run's `git push` is leak-scanned before it leaves: the commit messages, added lines and paths of the commits it sends, and annotated tag messages, but nothing the remote already has. A stored secret, a credential shape or a project forbidden-data pattern is refused with one line naming the commit, path and class, never the text; a later commit that removes it does not clear it.
