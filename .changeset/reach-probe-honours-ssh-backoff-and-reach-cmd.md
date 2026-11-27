---
"tt-project": patch
---

Machine reach probe: a machine in ssh backoff (after a refused connection start) is not probed and shows as backing off, not down. New `ssh.reach_cmd` (alias to shell command, exit 0 means reachable) replaces the ssh probe for hosts that limit connection starts. Outage alerts name the user's workstation tunnel only when its port refuses or nothing listens on it; a connection that was made and then closed, reset or left silent names the far host.
