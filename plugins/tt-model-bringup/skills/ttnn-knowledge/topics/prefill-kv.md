# Prefill, KV and paging

**Resumed or unaligned prefill differs from a fresh one.** Continuation at starts that are not chunk-aligned
failed accuracy while aligned starts passed. Three causes stacked in one model: the legacy rotary-embedding op
gave one core group a default compute config (use the HF-style rotary op in prefill); SDPA's Q/K chunk partition
depends on the start's alignment; and ring reduce-scatter or all-gather accumulation order depends on the
absolute tile-row phase. The same mechanism came back later under a new precision policy, so re-test resumed
starts after any precision or SDPA config change.

**Program keys from offsets and lengths.** Exact-length uploads, last-token slices, page-table suffix slices, a
scalar chunk offset and a shrinking fill window all become compile-time keys. Each new length or offset then
compiles, and after trace capture that releases or invalidates traces. What helped: a finite set of physical
buckets with device-side validity masks, tensor-valued offsets and row indices, and fixed-width page-table views.
For models with recurrent state, padding a prompt to a bucket must not advance the state: mask it.

**Paged SDPA reads past the table.** SDPA rounds its read window up to the chunk, so a chunk that does not divide
the page-table capacity reads page ids beyond the request's pages (garbage or NaN). Choose a chunk that divides
the capacity, and pad attention page tables with a valid page id rather than leaving tails uninitialized.
Padding bits are not page metadata.

**Plugin chunking.** The vLLM plugin can change a continuing request's row and slot between prefill chunks.
State that does not live in vLLM pages (recurrent state, conv history) then needs its own ownership check
(record which tokens and pages a slot's state covers). On preemption resume, one plugin version appended
replacement blocks in reversed order.

**Hybrid KV normalization.** vLLM's hybrid KV coordinator can normalize block size across layer groups and share
storage between layers through a tensor index; an adapter that assumes physical pages of the original size and
ignores that index loses the sharing or rejects the cache.

**Prefix reuse.** Sliding-window tail blocks can evict global-prefix blocks under the default eviction order,
dropping late-turn reuse; a larger measured pool restored it. Recurrent-state models cannot rebuild state from
KV blocks; reuse then comes from session continuation (`vllm-integration`, prefill features).

**Short and edge-length prompts.** Very short prefills (a single tile row) can take a decode-style on-chip memory
path that collides with prefill scratch; one model fixed it by never running prefill below a minimum row count.
Lengths at chunk edges and very long single prompts are where bugs cluster.

**Long prompts stall concurrent decode** when intermediate chunks run padded to the largest canonical size even
when few rows are consumed; running the smallest prepared profile that fits the consumed rows cut the stall.
