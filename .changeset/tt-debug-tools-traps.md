---
"tt-debug-tools": patch
---

Add debugging traps the skills lacked. `tt-profiler`: a long pipeline overflows
the marker buffer and loses its tail stages (capture per stage, merge off the
device); `DeviceZoneScopedN` names must be compile-time literals, a break that
only shows in a profiler build; compare medians of warm runs. `tt-watcher`: the
active-ethernet config-buffer overflow at fabric init and
`TT_METAL_WATCHER_DISABLE_ETH=1` for Tensix bugs; a short deadlock checklist in
the waypoints reference.
