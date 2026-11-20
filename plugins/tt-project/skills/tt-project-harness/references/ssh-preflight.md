# SSH preflight

`ttp ssh-preflight <host>` or `@<resource>` (the host from config `device.hosts` or `device.runners.<name>.host`, looked up each run) runs `ssh <host> true` with `BatchMode=yes`, `StrictHostKeyChecking=yes`, `ControlMaster=no` and `ControlPath=none`.
So neither a lax `StrictHostKeyChecking` in the user's ssh config nor an open shared connection hides a box that is gone or presents another key.
Exits: 0 reachable; 10 host key did not verify (changed, or not in known_hosts); 11 authentication failed; 13 this process cannot gain privileges and the host's ProxyCommand runs sudo (below); 255 not reachable (or 12 with `--classified`, for scripts that want a code of their own); 2 an unknown `@<resource>`.
As a `retry_when` or `start_when`, 255 keeps the task asleep and counts towards the network-outage alert, while 10, 11 and 13 wake it as a broken probe: waiting will not fix them. So does a 255 whose host name does not exist (no ssh Host entry defines it): check a new route with `ssh -G <alias>` before a probe or guard uses it.
Use it instead of a local ssh wrapper.

## A sudo ProxyCommand where nothing can gain privileges

Signature: ssh fails at once with `kex_exchange_identification: Connection closed by remote host`. It reads like the host or its `MaxStartups` refusing, but the host never saw the connection.
Cause: the process runs with no_new_privs (`NoNewPrivs: 1` in `/proc/self/status`), and the host's ssh config has a `ProxyCommand` that runs sudo (for example to reach a network namespace). no_new_privs makes sudo fail at once, the proxy closes, and ssh reports the closed pipe as the remote end. A sandboxed worker run has no_new_privs, and so does every `ttp lock` command and `ttp detach` job such a run starts. Calls work only while a ControlMaster connection opened outside is still alive, which makes the failure look intermittent.
Runs are unsandboxed by default, so this hits only a project that turns its provider's sandbox back on (`providers.<name>.write_fence: true`).
`ttp ssh-preflight` names it: when ssh fails, no_new_privs is set and `ssh -G <host>` shows a ProxyCommand running sudo (or doas, pkexec, run0), it exits 13 and says so. In a sandboxed Claude run, a failed command whose error has `kex_exchange_identification` gets the same hint.
Fixes, any one:
- Stay unsandboxed (the default), or leave the sandbox off for the project that needs this route.
- Run that ssh outside the sandbox: a model-free `kind: command` schedule or pre-step writes the files the run reads ([host-reaching-commands.md](host-reaching-commands.md)).
- A forwarder that needs no privileges: a `ProxyCommand` without sudo (a jump host via `ProxyJump`, or a forwarder the host runs as a service, where the process lives in the namespace and the ProxyCommand only connects to it).
- A ControlMaster connection opened outside the job (`ssh -o ControlMaster=yes -o ControlPersist=<time> -fN <host>`, from an unsandboxed shell or schedule): sessions in the job reuse it through its `ControlPath` and never run the ProxyCommand. Once it ends, the next call fails again, so open it for as long as the job runs. The preflight itself never uses a shared connection.
