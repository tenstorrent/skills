---
"tt-project": patch
---

tt-project 0.2.1004: runs on macOS no longer die when the OS refuses `os.nice`. Under `nice -n 10` macOS can refuse `os.nice(0)` with EPERM, and the runner's priority check crashed the run with PermissionError; the check and `renice` now read the level with `getpriority`, and the check only informs. A `start_when` wait that passed is booked to its gate in throughput, not to worker slots. Tests: a suite run inside a tt-project worker starts without the run's variables (TTP_TASK, TTP_RUN_DIR, TTP_NO_DEVICE, the agent CLI's session), keeping the checks harness's slot, port and output variables; four Mac-only test failures are fixed (BSD `nice` and `find`, the 104-byte AF_UNIX path cap, the sandbox probe's Linux check). setup-box's account migration lists the folders to rebuild without GNU find's `-printf`, so it also runs on BSD find.
