---
"tt-project": patch
---

`tt-project`: the credential folder guard no longer exempts interpreter code (python, node, perl, ...) that uses a key as an ssh or git identity: such code that names a credential path and holds a write word is refused again, and the refusal names the route that passes (run ssh or git as a plain shell command, or set `IdentityFile` in an ssh config). A shell line that uses a key passes only as `ssh|scp|sftp -i KEY ...` or `GIT_SSH_COMMAND='ssh -i KEY' git fetch|clone|ls-remote ...` with nothing beside it that can run a local program or write (another variable, git `-c` or `--upload-pack`, ssh `-F` or `-o ProxyCommand`, command substitution, a redirect to a built target, or a command other than a plain one such as `cd` or `echo`). The guide lists the accepted gaps.
