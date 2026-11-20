---
"tt-project": patch
---

`tt-project`: a run labelled `untrusted_input` may now push any ref to a local repository under its own temp folders (`$TMPDIR` or the run folder), such as a bare repo a repo test made for itself, as a plain path or a `file://` URL. The pre-push hook checks the URL after insteadOf rewrites and resolves symlinks and `..`. Still refused: network remotes (ssh, https, git://, scp-style, `<transport>::`), paths elsewhere, the pushing repository, the project's repository and its worktrees' git dirs, remotes rewritten to a network URL, any push that names its own receive-pack command, and a `$TMPDIR` that holds the whole disk, the home folder or the project, or that lies in the home folder outside the run folder.
