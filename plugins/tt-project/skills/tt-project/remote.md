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
  stale session from before a network change or sleep still holding the port) recovers by itself:
  one cleanup login ends only the box account's own sshd session, found by the `agent.<pid>` its
  agent link still points to (when that agent no longer answers) or by the port it holds with
  this workstation's tag for the same box and agent. It counts as done only once the ports are
  free; else it logs the held port and its pid and cleans again after 5, 20 and 60 s before the
  supervisor takes over. Nothing to do by hand; `ttp bridge doctor` names a held port, and
  `ttp setup`/upgrade refresh changed services.
- Self-heal watch: `ttp bridge setup` (and every refresh) installs one watcher per workstation
  (launchd every 30 s and on a network change on macOS, a systemd user timer every 30 s on Linux)
  running `ttp bridge watch`. Each pass logs in once per box and checks every forwarded port end
  to end (it reads the first byte the far service sends, so a watched port must speak first, like
  sshd). A dead port on a service meant to run gets the same cleanup (only this account's sshd
  session holding exactly that port, TERM then KILL; a listener `ss` hides is left alone and
  reported as an owner that cannot be identified, no other session is paused or signalled), then a
  restart (`launchctl kickstart -k` / `systemctl --user restart`, after `reset-failed` for a unit
  whose start limit was hit), paced 0, 60, 120, 300 s apart. A forward whose target does not
  answer from this computer is left alone (logged once; nothing ended or restarted); once it
  answers again, a port that still fails gets one restart, even while the session's other ports
  answer (a session rebuilt while the target was off can stay broken for it). An
  unreachable box is never left to a network change alone: it is checked again within 30-60 s, each
  attempt is logged, and its bridge services are restarted at most once per the same 0/60/120/300 s
  pace (each restart re-reads the ssh route and jump hosts and resolves the name again). A box
  down 5 minutes gets one desktop alert, and one more note with the outage length once it answers
  again. A stopped service is left alone. Hand-made ssh forward services join with a
  `"watch"` list in the local bridge config, never shipped with entries:
  `"watch": [{"label": "<launchd label or systemd unit>", "box": "<ssh alias>", "port": <box port>}]`,
  optionally with `"target": "<host>:<port>"` (what the forward reaches) and `"interface": "<name>"`
  (the one this computer reaches that target through): a forward whose target does not answer is
  then left alone the same way. An entry with no target whose port still connects without
  answering after one restart is not restarted again until its check changes or the network
  changes on macOS (logged once). A destination whose name does not resolve from this computer
  is never probed at all, so a probe can never hang on its lookup; it is left alone and logged
  once like a target that is off.
  Known limits: the watch does not detect a wake (a sleep longer than the 60 s backoff ends it, so
  the first pass after waking checks every box); an entry with no target is healed best effort;
  boxes are checked one after another within a pass; a listener whose owner `ss` hides is never
  freed.
  The checks of a target open and close a TCP connection to it each pass, which a strict
  fail2ban on the target's sshd may count.
  Each heal is one JSON line in `~/.tt-project/bridge-heal.log`; `ttp bridge status` shows the
  last three, and `~/.tt-project/bridge-watch.log` has every pass. Optional for a box admin, never
  done by tt-project: `ClientAliveInterval`/`ClientAliveCountMax` in sshd makes the box drop dead
  sessions itself.
- A `ttp bridge` session never relies on ssh exiting by itself: open forwarded-agent channels can
  keep ssh up after its remote command ended, holding connections to the local agent until the
  agent runs out of open files. The relay ends ssh with its whole process group (SIGTERM, then
  SIGKILL) once the remote command ended, or once the local agent it forwards stops answering a
  timed `ssh-add -l` twice in a row, logs one line saying why, and reconnects with backoff.
- Opt-in, never a default: a bridge agent may forward a dedicated agent instead of the login
  agent, so a burst of forwarded connections cannot exhaust the agent everything else uses. The
  user starts it themselves with a raised open-file limit (for example
  `sh -c 'ulimit -n <limit>; exec ssh-agent -D -a <socket path>'` under their own login service),
  loads its keys themselves with `SSH_AUTH_SOCK=<socket path> ssh-add`, and sets that socket path
  as the agent's `"agent"` in the bridge config. tt-project never creates, copies or loads keys
  for it.

## When a task needs a path between machines

| Need | Shape | Ask first |
|---|---|---|
| Laptop reaches a service on a box | local forward: `ssh -N -L <p>:127.0.0.1:<p> <box>` | no |
| Box reaches a service on the laptop | reverse forward from the laptop: `ssh -N -R <p>:127.0.0.1:<p> <box>` | yes |
| Box A reaches box B via the laptop | reverse forward on A to B's port: `ssh -N -R <p>:<B>:<port> <A>` | yes |
| Hop through a gateway | `ProxyJump <gateway>` in the ssh config | no |

- Prefer the user's existing ssh config aliases. NEVER invent hostnames.
- Record every tunnel the project depends on as a memory (`resource`).
