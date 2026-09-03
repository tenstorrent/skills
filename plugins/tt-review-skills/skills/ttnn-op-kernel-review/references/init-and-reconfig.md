# Category 1 — init and data-format reconfiguration

Every compute operation needs hardware initialisation with the correct CB indices. Helpers do this
by default; responsibility shifts to the caller under non-default policies or chained operations.

## 1.1 Hardware startup

`compute_kernel_hw_startup(icb0, icb1, ocb)` must be the **first call** in every compute kernel,
before any helper or raw API use. It configures the UNPACK, MATH and PACK units.

Check: is it called at the top of `kernel_main()`? Are the CB indices right — `icb0` = srcA input,
`icb1` = srcB input (or the same as `icb0` in the two-argument form), `ocb` = output? If the first
operation uses different CBs than startup was given, the hardware is misconfigured.

**Failure:** missing startup → TRISC hang with uninterpretable triage. Wrong CB indices → silent
numerical corruption, because the unpacker is configured for the wrong data format.

## 1.2 Operation-specific inits

| Operation | Init required |
|---|---|
| Reduce | `reduce_init<PoolType, ReduceDim>(icb, scaler_cb, ocb)` |
| Tilize | `tilize_init(icb, block, ocb)` |
| Untilize | `untilize_init(icb)` |
| Matmul | `mm_init(in0_cb, in1_cb, out_cb, transpose)` |
| SFPU unary | `init_sfpu(icb, ocb)` plus the per-op init, e.g. `recip_tile_init()` |

Check: does every operation have its init before first use? Are the CB indices in the init the CBs
that operation actually touches? For SFPU, is the per-operation init present as well as
`init_sfpu`?

**Failure:** missing init → undefined results with no compile error and no hang. Silent corruption.

## 1.3 Reconfiguration between operations

When two sequential operations use CBs with different data formats, unpacker and packer must be
reconfigured between them. Helpers do this via `DataFormatReconfig` (default `INPUT_AND_OUTPUT`).

Check: when a helper is called with `DataFormatReconfig::NONE` or `::INPUT`, does the next
operation's CB have a different output format? If so a manual `reconfig_data_format()` belongs
between them. When `InitUninitMode` chaining is used for back-to-back calls, is the chain complete
— first `InitOnly`, middle `Neither`, last `UninitOnly`?

**Failure:** missing reconfig → unpacker reads the CB in the wrong format → corruption. A broken
init/uninit chain leaves the hardware partially configured.

## 1.4 Reduce uninit

After a reduce, the packer retains reduce-specific edge masks. If the next operation is not a reduce
— or is a reduce on a different dimension — `reduce_uninit()` must come first.

Check: after the last `reduce()` or raw `reduce_tile()`, is `reduce_uninit()` called before any
non-reduce operation? The reduce helper handles this under the default `InitUninitMode`.

**Failure:** the next operation's pack inherits the reduce masks, so the wrong rows or columns are
masked in the output. Corruption, not a crash.

## Before flagging

Check the helper's policy first. Under defaults the helper does the init, the reconfig and the
uninit, and flagging it is a false positive that signals the reviewer has not read the helper. Flag
only when the policy is non-default, or when raw APIs are used without init.
