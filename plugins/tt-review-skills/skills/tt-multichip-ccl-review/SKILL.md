---
name: tt-multichip-ccl-review
description: Reviews multi-chip and collective-communication code — num_links against device topology, bias-before-all-reduce numerics, distributed RMSNorm correctness, gather and reduce axes, fabric initialisation order, and CCL tuning claims. Use when reviewing changes touching CCL ops, mesh devices, fabric, or any all_gather / reduce_scatter / all_reduce path.
metadata:
  tier: model
  upstream:
    - repo: tenstorrent/tt-buddy
      ref: ba9021417442d59756aa8cdf154a25648c9a0de5
      path: knowledge/ccl.md
    - repo: tenstorrent/tt-metal
      ref: d58cb341c703310cf41b5d88baafc0790ec0270b
      path: .agents/skills/multichip/SKILL.md
      branch: agentic-research/fast-models-fast
    - repo: tenstorrent/tt-metal
      ref: ce91f33c0c7184618d60553e4b32910c5ebdbfaa
      path: .github/bug_checker/rules/ccl-ring-buffer-mismatch.md
---

# Multi-chip and CCL review

Assumes `tt-review-core`. The failures here are device hangs and silent numerical errors, so
severity skews high.

## `num_links` must match the physical topology

`ttnn.experimental.all_gather_async(..., num_links=N, ...)` and siblings require `N` to match the
device topology's physical ethernet link count:

| Part | `num_links` |
|---|---|
| N150, 1 chip | N/A — no CCL |
| N300, 2 chips, ring | 1 |
| Galaxy, 32 chips, ring | 4 |
| TG (Tensor Galaxy) | per-axis; see the model's `tt_ccl.py` |

**A wrong value deadlocks the device** — a watchdog hang, not a soft failure. Hardcoding Galaxy's
value on N300 has hung for 10+ minutes before timeout and needed a device reset.

So: **flag any hardcoded `num_links`.** The safe pattern is to take it from a sibling model's
`tt_ccl` module (`tt_transformers/tt/ccl.py` auto-detects). A literal that happens to be right for
the author's machine is still a finding, because it is wrong on every other part.

## Bias before a cross-device sum is multiplied by `num_devices`

When a matmul is followed by AllGather + FastReduceNC — or any sum-reduce across devices — fusing
bias via `ttnn.linear(bias=...)` adds it on **each** device, and the cross-device sum multiplies it
by `num_devices`.

The fix is to pre-divide:

```python
if args.num_devices > 1:
    bias_torch = state_dict[...] / args.num_devices
else:
    bias_torch = state_dict[...]
# Distinct cache filename (e.g. "bias_div") so the old cache is not reloaded.
```

The cache filename matters as much as the division — a correct pre-divide with a stale cache name
silently reloads the undivided weights, which looks exactly like the original bug. Check both.

The same trap applies to any reduced intermediate followed by a bias or scale add.

## Distributed RMSNorm

Where hidden activations are sharded across the normalised dimension, local RMSNorm computes
incorrect statistics. The distributed primitive triple is `ttnn.rms_norm_pre_all_gather` → an
all-gather of the stats → `ttnn.rms_norm_post_all_gather`.

**Correct RMSNorm is mandatory; distributed RMSNorm is not.** A replicated-activation stream plus
local RMSNorm is acceptable when it preserves the decoder chain layout and measures faster. Do not
flag the absence of the distributed path as a bug on its own — flag *incorrect statistics*, which
means checking whether the normalised dimension is actually sharded.

## Gather and reduce axes

Check the axis, not just the op. On a 2D mesh, gathering the wrong axis produces correctly-shaped
output with wrong content, and PCC catches it only if the test covers multi-device. Confirm the axis
against the sharding of the tensor being gathered, and confirm cluster axis conventions against a
sibling model rather than assuming.

## Initialisation order

Fabric must be configured **before** `open_mesh_device`. A change that reorders device setup, or
introduces a helper that opens the mesh earlier, is a finding even if it currently works — it
depends on initialisation ordering that is easy to perturb.

## Watcher with async CCL

Watcher and async CCL interact badly enough that a hang under watcher is not automatically evidence
of a CCL bug. If a finding rests on a watcher-enabled run, say so, and downgrade one severity step
per `tt-review-core`'s ground-or-abstain rule.

## EDM channel configuration

Ring CCL passes data through ethernet data movers whose buffer counts, sizes and semaphore vectors
must agree between host setup and device kernel arguments, and between the two ends of a channel.
Host/device disagreements here produce out-of-bounds access, silent corruption, or a hang, and no
type system catches them.

See `references/edm-channel-consistency.md` — `num_buffers_per_channel` agreement, buffer/semaphore
vector lengths, `eth_buffer_size_bytes` drift, ring size versus device count, and sender/receiver
symmetry.

## Tuning knobs are non-monotonic

`chunks_per_sync`, `num_workers_per_link`, `num_buffers_per_channel` do **not** improve
monotonically. Measured on Gemma3 N300: `chunks_per_sync` 10 → 4 improved by 309 us, then 4 → 2
regressed by 976 us; `num_workers_per_link` 2 → 4 improved, 4 → 8 was neutral and wasted cores;
`num_buffers_per_channel` 2 → 4 regressed by 47 us.

Consequence for review: **a directional argument about these knobs is not evidence.** "Increasing
workers should help" is not a reason to accept or reject a value. Ask for the small targeted sweep
— three or four values per knob — and treat an unswept knob change as `CONSIDER`, not as a
regression you can assert.
