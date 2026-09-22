# Printing tiles out of a circular buffer

`TileSlice` reads a rectangular slice out of one tile of a CB and formats it.
`TSLICE` is the short form for the common case — an alias that fills in the
`cb_type` and `ptr_type` from the calling RISC's implicit choice. Unpacker and
compute have unambiguous ones (input CB, read pointer). Data movement does not.

```c++
cb_wait_front(CBIndex::c_25, 1);
DPRINT("{}\n", TSLICE(CBIndex::c_25, 0, SliceRange::hw0_32_16()));
cb_pop_front(CBIndex::c_25, 1);
```

The macro is only valid on RISCs where those defaults hold. On BRISC and
NCRISC the call has to use the constructor form with both extra arguments —
`TileSlice(cb, idx, range, TSLICE_INPUT_CB, TSLICE_RD_PTR)` — because
data-movement kernels can read from or write to either kind of CB.

## Arguments

| Argument | Type | Meaning |
|---|---|---|
| `cb_id` | `uint8_t` | Which circular buffer |
| `tile_idx` | `int` | Which tile inside it |
| `slice_range` | `SliceRange` | `h0`,`h1`,`hs`,`w0`,`w1`,`ws`, all `uint8_t` — start, end, stride per axis |
| `cb_type` | `dprint_tslice_cb_t` | `TSLICE_INPUT_CB` or `TSLICE_OUTPUT_CB`. **Data-movement RISCs only** |
| `ptr_type` | `dprint_tslice_ptr_t` | `TSLICE_RD_PTR` (front) or `TSLICE_WR_PTR` (back). **Data-movement RISCs only** |
| `endl_rows` | `bool` | Newline between rows, default `true` |
| `print_untilized` | `bool` | Untilize while printing, default `true`, always done for block-float formats |

`SliceRange` is a numpy-style slice: `{.h0=r, .h1=r+1, .hs=1, .w0=0, .w1=32, .ws=1}`
is row `r` of the tile.

## The pointer is what gets sampled

Not the tile index alone — the read or write pointer at the moment the print
executes. That fixes where the call has to sit:

| Reading from | Print must sit between |
|---|---|
| The front of the CB | `cb_wait_front` and `cb_pop_front` |
| The back of the CB | `cb_reserve_back` and `cb_push_back` |

Outside that window the pointer has moved on and the values printed belong to a
different tile. This is the failure that looks like a correctness bug in the
kernel rather than a misplaced print.

## Which RISC can print what

| RISC | Pointers available | Extra arguments |
|---|---|---|
| Data movement (`DPRINT_DATA0/1`) | Read and write | `cb_type` **and** `ptr_type` required |
| Unpacker (`DPRINT_UNPACK`) | Read only, input CBs only | None |
| Packer (`DPRINT_PACK`) | Write only | None |
| Math (`DPRINT_MATH`) | **No CB access at all** | — |

`DPRINT_MATH` with a `TSLICE` is invalid, not merely empty: the math RISC cannot
reach circular buffers.

```c++
for (int32_t r = 0; r < 32; ++r) {
    SliceRange sr = SliceRange{.h0 = r, .h1 = r+1, .hs = 1, .w0 = 0, .w1 = 32, .ws = 1};
    DPRINT_DATA0("{} {}\n", (uint)r, TileSlice(0, 0, sr, TSLICE_INPUT_CB, TSLICE_RD_PTR, true, false));
    DPRINT_UNPACK("{} {}\n", (uint)r, TileSlice(0, 0, sr, true, false));
}
```

## Supported data formats

`Float32`, `Float16_b`, `Bfp8_b`, `Bfp4_b`, `Int8`, `UInt8`, `UInt16`, `Int32`,
`UInt32`. A CB in any other format is not printable this way; the format is a
property of the CB, so check it before concluding the tile is empty.

## The output truncates at 64 bytes by default

`TileSlice` reserves `MAX_BYTES` bytes in the print buffer, defaulting to 64 —
which is about 32 values in BF16 or Float16_b, 16 in Float32 or UInt32, and 64
in the 8-bit integer formats. A slice range wider than that is silently
truncated; the numbers still line up until they abruptly stop. The row-by-row
loop above is the fix for a full tile; a wider single slice can raise the
template parameter, e.g. `TileSlice<128>(...)`.

## Cost

A full tile is 32 prints of 32 values, on every core selected and every RISC
selected, through a polled host server. Print one row, or a strided slice, before
printing a whole tile — and narrow `TT_METAL_DPRINT_CORES` and
`TT_METAL_DPRINT_RISCVS` first, or the output interleaves across cores and the
run slows enough to change the timing you may be investigating.
