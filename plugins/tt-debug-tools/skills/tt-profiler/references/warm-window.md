# Profile the warm window, not the load

Tracy profiles the whole process. Model construction, weight load and the
first-run JIT compile land in the same capture as the forward you care about,
and the device-side columns count them too. Warm-up skews more than host times.

## What the cold phase looks like

One-time weight preparation runs as ordinary device ops. `prepare_conv3d_weights`,
for example, issues a pair of `PermuteDeviceOperation`s per conv on every
device, and weights get `Tilize`d on the way in. Over a whole-process capture
these can top the summed `DEVICE FW DURATION [ns]` ranking and read as the
steady-state bottleneck when they ran once, at load.

If permutes, tilizes or other layout ops lead a ranking, check whether they
also run inside the warm forward before optimising them.

## Default: measure one warm forward

Unless the question is about cold start, compile or load time, write the
harness this way:

1. Construct, load, and run at least one untimed warm-up forward. That fills
   the program cache, lazy state and prepared weights.
2. `ttnn.synchronize_device(mesh)`, then `ttnn.ReadDeviceProfiler(mesh)`, so
   the load and warm-up zones are drained up to a known point.
3. Run one more forward, synchronise and read again. Analyse only that window.

When the harness cannot draw that line, mark it instead:

- a Tracy signpost around the warm forward (`from tracy import signpost`;
  `signpost(header="warm")`). The ops report carries signposts, so the window
  can be cut on them;
- or keep only rows whose `HOST START TS` is after the warm-up.

Profile the cold phase only when the caller asks about cold-start, compile or
load time. When in doubt, profile warm.
