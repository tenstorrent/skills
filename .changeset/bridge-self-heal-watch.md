---
"tt-project": patch
---

`tt-project`: bridges heal themselves after sleep or a network change. `ttp bridge setup` installs one watcher per workstation (launchd every 30 s and on a network change, or a systemd user timer) that checks each forwarded port on the box end to end; a dead port frees only this account's stale sshd session holding it (found even when `ss` hides its pid) and restarts the service, paced, with backoff while the box is unreachable. Hand-made forward services join through a `"watch"` list in the local bridge config; heals go to `bridge-heal.log` and show in `ttp bridge status`.
