# Waypoints

A waypoint is the last labelled point a RISC reached. The dump prints five per
worker core, in the legend's order.

There are close to two hundred codes and they are arch-specific, so do not work
from a memorised list. Resolve any code against the active checkout:

```bash
grep -rn 'WAYPOINT("NAWW")' $TT_METAL_HOME/tt_metal/
```

The call site is the answer — the code sits on the line before the loop it
labels.

## The decode rule

Multi-letter codes are `<operation><state>`, and the last letter is the state:

- **`W` — waiting.** Set immediately *before* a spin loop. A core showing `…W`
  is parked in that loop right now.
- **`D` — done.** Set immediately *after* the loop. The core got through.

`cb_wait_front` is the shape of all of them:

```c
WAYPOINT("CWFW");
do { … } while (pages_received < num_pages);
WAYPOINT("CWFD");
```

So `CWFW` and `CWFD` are the same call site; only one says the core is stuck.
This is the single most useful thing to know about a dump: read the trailing
letter first, then decode what it was doing.

## Single letters

From the legend, which is generated per part and is authoritative:

| Code | Meaning |
|---|---|
| `I` | Initialisation sequence |
| `W` | Wait, at the top of the firmware spin loop |
| `R` | Run, entering the kernel |
| `D` | Done, finished the spin loop |
| `X` | Host-written value, before firmware launch |
| `GW` / `GD` | Waiting for the go signal / go received |
| `K` | In a kernel |

`GW` on every core with `k_ids: 0` is an idle device between programs, not a
hang.

## Prefixes worth recognising

| Prefix | Family | Example |
|---|---|---|
| `CWF` | `cb_wait_front` — consumer waiting for pages | `CWFW` |
| `CRB` | `cb_reserve_back` — producer waiting for space | `CRBW` |
| `NAR` / `NAW` | NoC async read / write issue | `NAWW` |
| `NRB` / `NWB` | NoC read / write barrier | `NRBW` |
| `NAT` | NoC atomic | `NATW` |
| `UP` | Unpacker, in the compute LLK | `UPMW` |
| `PS` / `PW` | Dispatch kernels — prefetcher and dispatcher, not the packer | `PSW` |

## Reading a stuck core

1. Take the trailing letter of each of the five codes. Anything ending `W` is a
   candidate; everything ending `D` is not.
2. A producer/consumer pair tells you which side is starved. `CRBW` on the writer
   means it is waiting for the reader to free pages; `CWFW` on the reader means it
   is waiting for the writer to push them. Both at once is a deadlock, and the
   circular buffer is where to look.
3. `NAWW` or `NRBW` is a transfer that never completed. Suspect the target
   coordinate before suspecting the NoC — a write to a core that does not exist
   never acknowledges, and that is what the sanitizer catches when enabled.
4. Resolve the exact code with the grep above before concluding. Two codes in the
   same family can sit in different functions.
