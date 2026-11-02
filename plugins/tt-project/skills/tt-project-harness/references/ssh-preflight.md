# SSH preflight

`ttp ssh-preflight <host>` or `@<resource>` (the host from config `device.hosts` or `device.runners.<name>.host`, looked up each run) runs `ssh <host> true` with `BatchMode=yes`, `StrictHostKeyChecking=yes`, `ControlMaster=no` and `ControlPath=none`.
So neither a lax `StrictHostKeyChecking` in the user's ssh config nor an open shared connection hides a box that is gone or presents another key.
Exits: 0 reachable; 10 host key did not verify (changed, or not in known_hosts); 11 authentication failed; 255 not reachable (or 12 with `--classified`, for scripts that want a code of their own); 2 an unknown `@<resource>`.
As a `retry_when` or `start_when`, 255 keeps the task asleep and counts towards the network-outage alert, while 10 and 11 wake it as a broken probe: waiting will not fix them. So does a 255 whose host name does not exist (no ssh Host entry defines it): check a new route with `ssh -G <alias>` before a probe or guard uses it.
Use it instead of a local ssh wrapper.
