# Tracing and the program cache

**Allocation or compilation after capture.** The usual causes, in the order they were found: programs that only
some prompt lengths or offsets use (see prefill-kv); buffers created lazily on first use of a slot or on the
first decode step; reset, prefill-chunk and sampling programs never run before the decode trace was recorded. A
replayed trace then reads memory a later program overwrote, which showed up as garbage from the second request.
What helped: create persistent buffers at initialization, warm every prefill shape and every slot path before
capture, run tests with `TT_METAL_TRACE_ALLOC_TRACKING=1`, and check that the program-cache count does not grow
across a mixed-length request loop after warmup.

**A retained eager output changes the capture.** Keeping the last eager warmup output alive lowers the L1
allocator frontier; some ops pick a different program (their hash depends on available space), so capture misses
the cache or overlaps L1. Releasing warmup outputs before capture fixed it.

**End capture on every path.** An exception inside a capture body that skips `end_trace_capture` leaves capture
active, and closing the mesh then hangs. Wrap capture in try/finally and release traces in a tolerant cleanup.

**Late kernels are hard to see.** Some program variants (for example certain flash-decoding prefill paths) compile
only for shapes the warmup skipped; the allocation tracker found them, a single short test did not.
