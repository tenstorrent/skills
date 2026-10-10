# Quasar status rules

Two columns. `quasar:as_written` is the analysed code unchanged on Quasar. `quasar:port`, only
when a Quasar port exists, is that port's implementation of the same function.

| Value | Meaning | Evidence required in `quasar:evidence` |
|---|---|---|
| `✅` | A Quasar test covers the same op, layout and dtype (memory placement may differ), or `run.json` records a passing port e2e run that executes it | test path, or "port e2e" plus its `run.json` date |
| `⚠️` | Code path exists (Metal 2.0 mainline factory or a Quasar fork) but this variant has no Quasar test | the factory or fork path |
| `❌` | Fails validation or hits TT_FATAL on Quasar | the check that fails |
| `variant not used by port` | The port implements the function differently (for example height- instead of block-sharded) | what the port uses instead |
| `n/a` | The port never reaches this path | why |

Facts to verify at the pinned commit before relying on them:

- Block-float formats: `is_supported_quasar` in `tt_metal/common/tt_backend_api_types.cpp` lists
  the formats Quasar accepts; at the time of writing it has no `Bfp8_b`/`Bfp4_b`.
- Legacy kernels: `DataMovementKernel`/`ComputeKernel` constructors in
  `tt_metal/impl/kernels/kernel.hpp` TT_FATAL on Quasar, so ProgramDescriptor/CreateKernel
  factories cannot run there; only Metal 2.0 ProgramSpec factories lower to Quasar kernels.
- Quasar forks live under `ttnn/cpp/ttnn/operations/experimental/quasar/` and are exposed as
  `ttnn.experimental.quasar.*`; mainline calls are not routed to them automatically.
- Quasar op tests are mostly outside CI (`tests/scripts/quasar/*.yaml`); say so when a ✅ rests on
  a locally run test.
