---
"tt-project": patch
---

`tt-project`: the setup-box skill's `recovery.md` adds three Blackhole Galaxy rules. On a box a tenant is using, only reads that open no device run; anything that opens a `/dev/tenstorrent` handle, resets or reloads the driver waits for the tenant's owner. BMC access is reads only unless the single-host power cycle (op 0x3) is called for, and the vendor `agera2-app` tool is never run. The single-host power cycle is only for a host serving nothing; on a host that serves a tenant, the tenant's owner decides.
