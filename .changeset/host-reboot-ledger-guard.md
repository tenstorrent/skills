---
"tt-project": patch
---

`tt-project`: new rule check `host_reboot`, on by default for every provider. A run's command that reboots, halts or powers off a host (`reboot`, `shutdown`, `poweroff`, `systemctl reboot|poweroff|kexec`, `kexec -e`, a sysrq write, logind's Reboot over D-Bus, an `ipmitool` power cycle or an http power-cycle call), here or over ssh (its command or a heredoc fed to it), is refused unless an open power-cycle intent for that host is on the machine-change ledger, holds now and was recorded `guards.reboot_lead_s` (default 120 s) earlier. The project that records an intent announces it in its chat as a low alert.
