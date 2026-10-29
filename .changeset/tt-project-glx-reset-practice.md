---
"tt-project": patch
---

The worker prompt and the tt-project-harness skill now state the Blackhole Galaxy reset practice:
reset a host's own chips only with `tt-smi -glx_reset` (never `tt-smi -r`, not even as a fallback),
check sudo with `sudo -n -l`, resets and single-host power cycles never affect other hosts, and after
a tray power drop try glx_reset first, then the single-host BMC power cycle with op 0x3 only.
