---
"tt-project": patch
---

`tt-project`: bridge sessions and kept web tunnels (launchd agents and systemd user units) now wait a random 0-60 s (`tunnel.RECONNECT_SPREAD_S`) before ssh on each restart by launchd or systemd. Before, every one reconnected in the same second after a sleep/wake or network change, and about 15 at once tripped the host's sshd `MaxStartups`, so logins failed with `kex_exchange_identification: read: Connection reset by peer`. The wait is a POSIX `sh` wrapper reading `/dev/urandom` with `od`, which then execs the same ssh command; the start that installs or restarts a service skips it through a one-shot marker file under `~/.tt-project`. `ttp setup` and the upgrade re-install installed bridge services (their command changed) and kept tunnels without the wait, on the same port. ssh options (`ControlMaster=no`, `ControlPath=none`) are unchanged.
