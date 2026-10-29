# Reset and recovery on a Blackhole Galaxy

How a box task resets a Blackhole Galaxy around a device run and recovers a tray that dropped.
General device recovery is tt-buddy's (`skills/run/recovery.md`); this page adds the Galaxy practice.

## Reset

- The reset is `tt-smi -glx_reset` (with its root-port rescan and a recount by BDF and tray).
  `tt-smi -r` is never run on a Blackhole Galaxy, per the vendor's BH Galaxy reset documentation:
  it drops chips. Where glx_reset cannot run on a host, no reset runs and the output says so plainly.
- Run glx_reset before and after every device run.
- After glx_reset, rescan the PCIe root ports and recount the chips by BDF and tray
  (`lspci -d 1e52:`).

## Privileges

- glx_reset's IPMI step needs root or a NOPASSWD sudo rule for its exact commands; the root-port
  rescan needs root.
- Where such rules are granted (exabox, shared serving hosts), they cover the exact glx_reset IPMI
  commands and the diagnostics `dmesg -T`, `journalctl -k -b` and `lspci -d 1e52:`; on serving
  hosts also `tt-smi` and `ipmitool`.
- Check with `sudo -n -l`, never `sudo -n true`: a rule covers named commands, not a shell.
- Where glx_reset cannot run, no reset runs and the output says so.

## A tray that dropped

- After a tray power drop, still try glx_reset first, then the single-host BMC power cycle.
  The single-host power cycle is only for a host serving nothing (no tenant); on a host that
  serves one, the tenant's owner decides.
- Resets and single-host power cycles never affect other hosts: no gate, snapshot or serving-down
  check on neighbouring hosts. Only a quad serving a quad-wide workload goes down and up as a quad.
- For the BMC power cycle use op 0x3 only: op 0x0 also resets the PCIe retimers carrying NVMe.
- BMC access is reads only, unless these rules call for the single-host power cycle (op 0x3).
  The vendor `agera2-app` tool is never run.
- Tray temperature readings right after a power loss are junk: count only readings taken at least
  one poll after it.
- A cable short keeps a tray off from power-on; a bus bar drops it under load.

## Tenants

- Idle Blackhole chips draw about 50-77 W, so power draw alone shows no tenant.
- A tenant is an open `/dev/tenstorrent` handle or a tenant process or container. A fabric-manager
  agent or a DRA driver is plumbing, not a tenant.
- On a box a tenant is using, only reads that open no device may run (listing `/dev/tenstorrent`,
  reading logs, sysfs or `lspci` reads). Anything that opens a `/dev/tenstorrent` handle, resets or reloads
  the driver waits for the tenant's owner.
