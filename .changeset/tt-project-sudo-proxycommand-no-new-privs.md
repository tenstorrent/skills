---
"tt-project": patch
---

`tt-project`: an ssh whose ProxyCommand runs sudo, failing where nothing can gain privileges (a sandboxed run and its `ttp lock` and `ttp detach` jobs), is named instead of looking like the host refusing. `ttp ssh-preflight` exits 13 (which wakes a waiting task) when ssh's proxy died before any banner, this process has no_new_privs and `ssh -G <host>` shows a ProxyCommand running sudo, doas, pkexec or run0, and says the cause and the fixes. In a sandboxed Claude run a failed command whose error has `kex_exchange_identification` gets the same hint. The tt-project-harness skill and its ssh-preflight reference document the signature, the cause and the supported forwarders: stay unsandboxed (the default), run that ssh from a model-free command schedule or pre-step, a ProxyCommand without sudo, or a ControlMaster opened outside the job.
