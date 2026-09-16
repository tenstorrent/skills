# Reading `profile_log_device.csv`

Two header lines, then one row per zone boundary. Parse the header row rather
than assuming column positions: the doc page still documents a 12-column layout
with a `zone phase` of `begin`/`end`, and the file has 15 columns and
`ZONE_START` / `ZONE_END`.

```
ARCH: wormhole_b0, CHIP_FREQ[MHz]: 1000, Max Compute Cores: 64
PCIe slot, core_x, core_y, RISC processor type, timer_id, time[cycles since reset], data, run host ID, trace id, trace id counter, zone name, type, source line, source file, meta data
```

| Column | Meaning |
|---|---|
| `PCIe slot` | Device |
| `core_x`, `core_y` | Core |
| `RISC processor type` | `BRISC`, `NCRISC`, `TRISC_0..2`, or an ethernet RISC |
| `timer_id` | Identifies the zone instance |
| `time[cycles since reset]` | **Cycles, not time.** Convert with `CHIP_FREQ[MHz]` from line 1 |
| `data` | Zone payload where one was recorded |
| `run host ID` | Which host-side run |
| `trace id`, `trace id counter` | Populated for traced execution, empty otherwise |
| `zone name` | Firmware envelope or your `DeviceZoneScopedN` name |
| `type` | `ZONE_START` or `ZONE_END` |
| `source line`, `source file` | Where the zone is declared |
| `meta data` | Usually empty |

## Getting a duration

A zone is two rows. Pair them on `(PCIe slot, core_x, core_y, RISC processor
type, timer_id, zone name)` and subtract the cycle counts, then divide by the
chip frequency:

```
duration_ns = (cycles_end - cycles_start) * 1000 / CHIP_FREQ_MHZ
```

At `CHIP_FREQ[MHz]: 1000` one cycle is one nanosecond, which makes the arithmetic
invisible — do not carry that assumption to a part clocked differently. Read the
frequency from line 1 every time.

## The zone names you get for free

Even a kernel with no annotations produces the firmware envelopes:

| Zone | Spans |
|---|---|
| `BRISC-FW`, `NCRISC-FW`, `TRISC-FW` | Firmware, per RISC |
| `BRISC-KERNEL`, `NCRISC-KERNEL`, `TRISC-KERNEL` | The kernel within it |

Your own zones appear alongside them — `TEST-FULL` in the `full_buffer` example.
The `-FW` zone minus the `-KERNEL` zone is firmware overhead, which is the first
thing to check before optimising a kernel that looks slow.

## Reading it without drowning

`test_full_buffer`, a `nop` loop, produces over 1.6 million rows. Never read the
file whole.

```bash
C=$TT_METAL_HOME/generated/profiler/.logs/profile_log_device.csv
head -2 "$C"                                            # arch, frequency, header
awk -F, 'NR>2 {gsub(/ /,"",$11); print $11}' "$C" | sort -u   # which zones exist
awk -F, 'NR>2 && $11 ~ /TEST-FULL/' "$C" | head         # one zone only
```

Filter by zone name and core first, then pair rows. A whole-file read is both
useless and expensive.

## Traps

**Cycles since reset, not since program start.** The absolute numbers are large
and only differences are meaningful. Two rows from different `run host ID`s are
not comparable as a duration.

**Empty `trace id` is normal** on an untraced run — it does not mean data is
missing.

**Whitespace padding.** Fields are space-padded in places (`BRISC-FW    `), so
compare trimmed, as the `gsub` above does.

**A missing `ZONE_END`** means the buffer filled or the device closed mid-zone.
An unmatched `ZONE_START` is not a hang — check whether results were read early
with `ReadDeviceProfilerResults`.
