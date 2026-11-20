---
"tt-project": patch
---

The credential check for runs that read untrusted text lets ssh, scp, sftp and git (core.sshCommand) read their own files: the value of -F and -i and of -o IdentityFile=, CertificateFile= and UserKnownHostsFile= is not treated as a credential read, also under `ttp lock <res> --`; reading a key with cat or cp, a -F naming a key, a ProxyCommand reading one and a remote command naming one stay refused. The stream guard no longer stops a Codex run for a refused command that had already finished when it read it: it records a warning and the daemon files one low event.
