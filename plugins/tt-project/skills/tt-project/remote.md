# Remote projects and tunnels

- Commands for a remote project forward over ssh automatically.
- The web app binds to localhost on its machine. Reach it with a local forward.
- A local forward to view a project's web app: the user's first one is asked once, per user
  (`ttp consent tunnel` says `ask`; record `ttp consent tunnel yes|no`, kept in ~/.tt-project). A
  kept tunnel already installed counts as a yes. After a yes, open it and keep it up without asking,
  tell the user it is open and give the link. After a no, never re-ask; give the command instead.
- Ask the user before any tunnel that exposes their machine to others: a reverse forward into
  the laptop, or a port bound on anything but localhost. Say what it connects and why.

## Web app from another machine

- `ttp web <name> --tunnel --keep` opens the local forward as a user service
  (`com.tt-project.tunnel.<name>`: launchd on macOS, systemd --user on Linux) that restarts it
  after reboots (at login) and network drops, then prints the link. Run it whenever the user
  wants the web app of a remote project.
- It adopts an existing `com.tt-project.tunnel.<name>` service that already forwards to the
  project; any other it replaces, keeping its local port so the link stays the same.
  `ttp web <name> --unkeep` removes it.
- `ttp web <name>` alone prints the forward command and the link without opening anything.
- The ssh login must work without a prompt (key or agent). On Linux the unit starts at login;
  it runs at boot without a login only with lingering (`loginctl enable-linger`).
- When the page cannot reach the daemon it says so and shows these commands as information.
- `ttp new --host` and `ttp connect` for a remote project open the same kept forward themselves and
  end with the link, checked through it (HTTP 200 naming the project). If it does not answer they
  restart the kept tunnel, then the daemon there, and otherwise print what is broken instead.

## Persistent tunnel rules

- `ExitOnForwardFailure=yes`: a forward that cannot bind must fail loudly.
- `ServerAliveInterval=30`, `ServerAliveCountMax=3`: drop dead links fast.
- `ControlMaster=no`, `ControlPath=none`: the tunnel owns its connection. As a client of a shared
  master it would hand the forward over and exit (the supervisor respawns it in a loop), and the
  forward would die with that master.
- Supervisor restarts it (`KeepAlive` / `Restart=always`).
- A tunnel that connects but binds nothing is broken. Verify from the far end.
- A `ttp bridge` session whose reverse forward fails (`remote port forwarding failed`, usually a
  stale session from before a network change still holding the port) recovers by itself: one
  cleanup login ends only the box account's own sshd session on that port carrying this
  workstation's tag for the same box and agent, then it retries once. Nothing to do by hand;
  `ttp bridge status --deep` names a held port, and `ttp setup`/upgrade refresh changed services.

## When a task needs a path between machines

| Need | Shape | Ask first |
|---|---|---|
| Laptop reaches a service on a box | local forward: `ssh -N -L <p>:127.0.0.1:<p> <box>` | no |
| Box reaches a service on the laptop | reverse forward from the laptop: `ssh -N -R <p>:127.0.0.1:<p> <box>` | yes |
| Box A reaches box B via the laptop | reverse forward on A to B's port: `ssh -N -R <p>:<B>:<port> <A>` | yes |
| Hop through a gateway | `ProxyJump <gateway>` in the ssh config | no |

- Prefer the user's existing ssh config aliases. NEVER invent hostnames.
- Record every tunnel the project depends on as a memory (`resource`).
