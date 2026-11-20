# Service changes in drivers

A worker never stops, restarts, disables or masks a shared service on a shared machine (a device broker, its recovery timers, a box's monitoring) unless its task says to: the change hits everyone on the machine, and a masked or disabled unit outlives the run.
Before running a driver, job script or test stub there, check it with `ttp svcscan <script>` (runtime `svcscan.py`): it prints each line that changes a unit and exits 1 if one does, 2 if a script cannot be read.
It flags every `systemctl` verb but a read-only query (`status`, `show`, `is-active`, `list-units`, ... in `READ_ONLY`), also under `sudo -n`, in Python argv lists and across backslash-continued lines, and `service <unit> stop|start|restart|reload|...`; comment lines are skipped.
To exercise the driver without changing a real unit, run `ttp svcscan --shim <dir>`, then `PATH=<dir>:"$PATH" <script>`: the `systemctl`, `service` and `sudo` stand-ins log each call to `<dir>/svcscan.log`, only read-only queries reach the real `systemctl`, and `sudo` runs nothing but the two stand-ins.
A call by path (`/usr/bin/systemctl`, `./systemctl`, `/usr/bin/sudo`) or over ssh is flagged as such, because a local shim does not stop it: edit it out, or run that step only where the task says the change is allowed.
`ttp svcscan --help` carries the same guidance; `ttp killscan <script>` is the matching check for kills by name or pattern.
