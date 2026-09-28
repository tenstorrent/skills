# Boards, ASICs and the device string

Shared by `tt:discover` (`boards.md`, D9/D10), `tt:retrieve` (`bundle.md` B7,
`SKILL.md` `--device`) and anything that names a device key.

The one rule everything here serves: **the device string is derived from the
hardware present, never read off a product page, a getting-started guide or
a container's `MESH_DEVICE`.**

## Boards vs ASICs

From the tt-smi snapshot (`discover/boards.md` § Capture):

| Quantity | Where it comes from |
|---|---|
| ASICs (chips) | number of entries in `device_info` |
| Physical boards | number of **distinct** `board_info.board_id` values |
| Board family | `board_info.board_type`, minus its trailing revision letter (`p300c` → `p300`) |

A board carries one or more ASICs, so these two counts are not
interchangeable. Every board of a dual-ASIC family contributes two
`device_info` entries with the same `board_id`.

## Board type → ASIC count → device string

```
device string = <FAMILY upper-case> + ("X" + <board count>  if boards > 1)
```

The suffix counts **boards, not chips**. This is the single most common
error in this area: `P300X2` and `P150X4` are both 4-ASIC hosts.

| Family | Arch | ASICs / board | 1 board | n boards | Verified |
|---|---|---|---|---|---|
| `p100` | Blackhole | 1 | `P100` | — | spec key union only |
| `p150` | Blackhole | 1 | `P150` | `P150X4`, `P150X8` | spec key union only |
| `p300` | Blackhole | 2 | `P300` | `P300X2` | **yes — 2026-09-22, this host: 4 ASICs / 2 boards, all `p300c` → `P300X2`** |
| `n150` | Wormhole b0 | 1 | `N150` | `N150X4` | spec key union only |
| `n300` | Wormhole b0 | 2 | `N300` | — | spec key union only |

Named multi-host systems do not follow the `XN` formula and are not derivable
from a single host's snapshot: `T3K` (8 Wormhole ASICs), `GALAXY`,
`GALAXY_T3K`, `DUAL_GALAXY`, `QUAD_GALAXY`. If the snapshot's counts do not
produce a key in the spec's device union, report the derived string and add
`not in spec` — do not substitute a named system that happens to have the
same chip count.

The union above is the set of keys actually present in
`release_model_spec.json`. Read it from the spec rather than from this file
whenever the spec is already fetched — this table decays, the spec does not.

## Doc naming

The spec uses the key **upper-case**; the per-model documentation path uses
it **lower-case**, and lower-cases the model type too:

```
docs/model_support/<model_type lower>/<model_name>_<device key lower>.md
```

- `model_type` is lower-cased, with one substitution: `text_to_speech` → `tts`.
  The directory is not always `llm/`.
- `model_name` is the spec's `model_name` field (e.g. `Qwen3.6-27B`) — **not**
  the HF repo id. Interpolating `Qwen/Qwen3.6-27B` puts a slash in the path
  and breaks the URL.
- The device suffix is the spec key lower-cased verbatim: `P300X2` → `p300x2`.

Example, for the entry verified on 2026-09-22:
`docs/model_support/llm/Qwen3.6-27B_p300x2.md`

## Rules

- Mixed `board_type` values in one snapshot → `unknown: mixed board types`,
  list them. Never pick the majority.
- A container's `MESH_DEVICE` (here `(1,4)`) is a mesh shape, not a device
  key. It describes how the chips were opened, not what the host is.
- The stack can misdetect the device: on 2026-09-22 this p300x2 host was
  logged as `P150x4` by the server (`MAX_PREFILL_CHUNK_SIZE` fell back to 4).
  Both are 4-ASIC, which is exactly why chip count alone cannot identify a
  host. Serials decide.
