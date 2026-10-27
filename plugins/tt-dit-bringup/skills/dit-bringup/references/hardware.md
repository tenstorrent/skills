# Target hardware

Default: **Blackhole LoudBox, 8 chips as a 2x4 mesh.** Use it unless the user names other
hardware or `tt-smi -ls` on the device host shows a different system.

## How in-tree models run on a 2x4 Blackhole LoudBox

Taken from tt_dit's existing tests; re-check the current tests of the closest in-tree model,
since these settings change as models are tuned.

- Performance tests (Flux 1, Flux 2, Wan 2.2, Ideogram 4) label mesh `(2, 4)` as `BH_LB` on
  Blackhole and `WH_T3K` on Wormhole, so the same test entry serves both.
- Topology is `ttnn.Topology.Linear`.
- `num_links` and the axis split are per model. For example:
  - Wan 2.2 has two 2x4 entries: sequence parallel on axis 0 and tensor parallel on axis 1
    with 1 link, or sequence parallel on axis 1 and tensor parallel on axis 0 with 2 links.
    Both load encoder and transformer in turn to fit memory.
  - Ideogram 4 uses sequence parallel 4 on axis 1 and tensor parallel 2 on axis 0, 2 links;
    its VAE uses tensor parallel 4.
- Some models need extra device params on Blackhole 2x4 (for example LTX sets an L1 small
  size and a trace region); copy them from the closest model's mesh params.

## Choosing the parallel config

1. Copy the closest in-tree model's `(2, 4)` test parametrization: axis split, `num_links`,
   topology, device/fabric params, and memory strategy.
2. If it has no `(2, 4)` entry, start from its 4x8 config with each axis scaled down, or from
   the 2x4 entry of the next-closest model.
3. Record the chosen config in `run.json` and the stage 1 summary.

## Other systems

| System | Mesh | Notes |
|---|---|---|
| Wormhole LoudBox, QuietBox, T3K | 2x4 | 8 chips; same test entries as Blackhole LoudBox |
| Blackhole QuietBox | 2x2 | 4 chips; tight memory, models use FSDP, staged loading, or lower resolution |
| Galaxy | 4x8 | 32 chips; some models use Ring topology here |

For these, follow the same steps with the matching entry in the closest model's tests.
