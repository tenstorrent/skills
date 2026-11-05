---
"tt-project": patch
---

`ttp hostcheck`: an ssh alias whose HostName is loopback (a tunnel end, or a ProxyJump/ProxyCommand hop into another machine) is a remote host reached through the project's access, whatever its port; only `local` and this machine's own names are checked here. A HostName that is a non-loopback address of this machine counts as this host only on port 22 with no proxy, read from one `ssh -G` (with the access's own ssh config file when it has one).
