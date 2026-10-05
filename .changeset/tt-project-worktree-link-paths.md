---
"tt-project": patch
---

New task worktrees get a symlink to the project checkout's git-ignored `.venv`, so project checks that run `.venv/bin/python` work there without a per-worktree venv. New config key `worktree.link_paths` (default `[".venv"]`, `[]` turns it off) lists the entries to link. Only entries that exist and are git-ignored in the checkout and are missing in the worktree are linked; tracked and existing paths are left alone, the link is kept out of commits through info/exclude when needed, and removing the worktree deletes only the link.
