---
"tt-project": patch
---

`tt-project`: the credential watch counts a credential writer (`gh auth` login/refresh/switch/logout/setup-git, `ssh-keygen` without a scratch `-f`, `git credential` approve/store/erase, `git config credential.helper`) or a write to a store only when the run issues it as a command: its own simple command, also behind `sudo`, `env`, `cd ... &&`, `bash -c` or a heredoc fed to a shell. The same words inside a grep pattern, a commit message, a heredoc only `cat` reads or a `pytest -k` argument no longer fail the run's hand-off, and a command after a shell comment or a quote spanning lines still counts.
