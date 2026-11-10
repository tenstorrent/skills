---
"tt-project": patch
---

`tt-project`: the credential-store and credential-folder guards let ssh read its own settings files in `~/.ssh` by any name. The direct value of `-F <config>`, `-o UserKnownHostsFile=` or `-o GlobalKnownHostsFile=` now passes in ssh, scp and sftp, and in the plain ssh command that rsync (`-e`) or git (`GIT_SSH_COMMAND`, `-c core.sshCommand=`) runs, also beside a key given by `-i` or `-o IdentityFile=`. The file must exist and must not be a private key by name or content. This extends the key path class rule; it adds no second exemption. A `-F` config outside `~/.ssh` still counts as running a local program, so a key beside it is still refused. Reading, copying or writing the same files (`cat`, `grep`, `cp`, scp with the file as a source, redirects, inline code) stays refused. The guide also says how the harness's own host checks reach a host whose route needs an ssh config: `coordinator.session.host_access.<alias>`.
