---
"tt-project": patch
---

`tt-project`: the account test pool no longer starts a check run on a spill host while a device job runs there. Before it places a run, it checks the host's device locks (`<alias>-device`, the device names of the host's machines-list `shared` entry, and `device.locks` names on it) for a holder, a reservation or a waiter, and reads the running job rows of any broker heal check the project has on that machine. A busy host is skipped for the next pool host, or the run waits for this machine's own slots. A broker status that cannot be read never blocks, and runs already on the host go on. `ttp status` and the web app show each skip in one line with the host and the reason.
