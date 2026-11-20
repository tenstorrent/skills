---
"tt-project": patch
---

`tt-project`: on macOS the daemon no longer starts a `sysctl` process for each boot check. Checking whether a recorded run, lock or push belongs to this boot read `kern.bootsessionuuid` and `kern.boottime` with one `sysctl` process each, many times a tick (`sysctl` was up to 36 of the 54-160 process starts a minute measured on live Mac projects). Both are now read in-process through sysctlbyname with the same text as `sysctl -n`, so boot ids recorded before still read as this boot. Linux reads /proc as before; other systems still run `sysctl`.
