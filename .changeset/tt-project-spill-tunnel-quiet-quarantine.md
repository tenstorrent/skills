---
"tt-project": patch
---

`tt-project`: a spill host taken out by its health check comes back only when a probe passes, never because its quarantine time ran out, so a host behind a down workstation tunnel no longer cycles through "back in the spill pool" and "failed its health check" events. Hosts that fail to connect through a forwarded loopback route go out quietly in every probe round, with one low tunnel-down event per outage (normal once it lasts an hour) and one low event when the tunnel is back; they get no per-host out or back events. Other host failures keep their own events, and a config fault on every host is still one high event that takes none out.
