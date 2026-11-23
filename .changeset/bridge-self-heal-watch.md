---
"tt-project": patch
---

`tt-project`: bridges heal themselves after sleep or a network change. `ttp bridge setup` installs one watcher per workstation (launchd every 30 s and on a network change, or a systemd user timer) that checks each forwarded port on the box end to end; a dead port frees only this account's stale sshd session holding it (a listener `ss` hides is left alone, never by pausing other sessions) and restarts the service (a failed systemd unit is reset first), paced, rechecking an unreachable box within a minute. A forward whose target does not answer from the computer is left alone. Hand-made forward services join through a `"watch"` list in the local bridge config, optionally naming the forward's target so a dead target is left alone too (one with no target is restarted once, then again only after its check changes or a wake or network change); all ports are checked at once; heals go to `bridge-heal.log` and show in `ttp bridge status`.
