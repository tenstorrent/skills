# Reading watcher state without a watcher run

Watcher's data structures live in L1 and in hardware registers, so they can be
read after the fact — including from a run that never enabled watcher. Two paths,
and they answer different questions.

What is missing when watcher was off: the **debug-only** state, waypoints above
all, because nothing was recording it. Mailboxes, run messages, kernel ids and
hardware registers still read.

## `watcher_dump`, against a device

A standalone binary, built with the metal tests as `build/tools/watcher_dump`.
It opens the device and dumps what is there now — no relaunch, no instrumentation
in the previous run.

```bash
build/tools/watcher_dump --devices=all --dump-watcher
```

| Option | Effect |
|---|---|
| `-d=LIST`, `--devices=LIST` | Comma-separated chip ids (`0,2,3`) or `all`. |
| `-w`, `--dump-watcher` | Watcher data. Content depends on what the original run enabled. |
| `-c`, `--dump-cqs` | Command-queue data. |
| `--dump-cqs-data` | Raw command-queue bytes. Minutes per queue. |
| `--dump-noc-transfer-data` | Only present if the run set `TT_METAL_RECORD_NOC_TRANSFER_DATA`. |
| `-n=INT`, `--num-hw-cqs=INT` | Must match the original program. |
| `--eth-dispatch` | Must match the original program. |

The last two are not conveniences: get them wrong and the tool reads the wrong
structures and reports confidently from them. Take both from the run being
investigated.

## `gdb`, against a **live** process

For a host-side failure — an assert or a segfault — where the process is
still up and the device is still holding the state that explains it.

Attach, interrupt with ctrl-c, then:

```
thread 1                              # be on the main thread
up                                    # repeat until the frame is in namespace tt
call tt::watcher::dump(stderr, true)  # true also dumps HW registers
```

The frame matters. The call has to resolve `tt::watcher::dump`, so keep going
`up` out of template and standard-library frames until the frame is in the `tt`
namespace. The function is deliberately marked `noinline` upstream so it survives
optimisation and remains callable this way.

Output goes to the process's stderr, in the same per-core shape as a log dump —
`references/log-format.md`.

**Core dumps cannot do this.** `call` needs a running process — it executes
the function against the live device to read hardware registers. A core dump
carries saved memory only; against one, `gdb` can `bt`, `p` locals, and inspect
recorded L1 addresses that made it into the core, but it cannot invoke
`tt::watcher::dump` or read the device. For a post-crash device read, use
`watcher_dump` above — it opens the device directly and does not need the
original process.

## Which to reach for

| Situation | Path |
|---|---|
| Host process is still up and stuck | `gdb --pid=$PID`, then `call tt::watcher::dump(...)` |
| Only a core dump left | `gdb core`, inspect with `bt`/`p`; run `watcher_dump` for the device state |
| Host process is gone entirely | `watcher_dump` |

Neither path resumes anything. Both read a device that some other process may
still own, so on a shared host confirm the run you are investigating is the one
holding it.
