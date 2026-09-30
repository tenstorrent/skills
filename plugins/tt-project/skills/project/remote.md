# Remote projects and tunnels

- Commands for a remote project forward over ssh automatically.
- The web app binds to localhost on its machine. Reach it with a local forward.
- ALWAYS ask the user before opening any tunnel. Say what it connects and why.

## Web app from another machine

- `ttp web <name>` prints the exact tunnel command (with a free local port) and the link.
- After the user agrees: `ttp web <name> --tunnel` opens it in the background.
- Persistent: only if the user asks. Use a user service (launchd / systemd --user).

## Persistent tunnel rules

- `ExitOnForwardFailure=yes`: a forward that cannot bind must fail loudly.
- `ServerAliveInterval=30`, `ServerAliveCountMax=3`: drop dead links fast.
- Supervisor restarts it (`KeepAlive` / `Restart=always`).
- A tunnel that connects but binds nothing is broken. Verify from the far end.

## When a task needs a path between machines

| Need | Shape |
|---|---|
| Box reaches a service on the laptop | reverse forward from the laptop: `ssh -N -R <p>:127.0.0.1:<p> <box>` |
| Laptop reaches a service on a box | local forward: `ssh -N -L <p>:127.0.0.1:<p> <box>` |
| Box A reaches box B via the laptop | reverse forward on A to B's port: `ssh -N -R <p>:<B>:<port> <A>` |
| Hop through a gateway | `ProxyJump <gateway>` in the ssh config |

- Prefer the user's existing ssh config aliases. NEVER invent hostnames.
- Record every tunnel the project depends on as a memory (`resource`).
