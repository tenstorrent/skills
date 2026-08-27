# tt-multichip-ccl-review

Reviews multi-chip and collective-communication code — num_links against device topology, bias-before-all-reduce numerics, distributed RMSNorm correctness, gather and reduce axes, fabric initialisation order, and CCL tuning claims. Use when reviewing changes touching CCL ops, mesh devices, fabric, or any all_gather / reduce_scatter / all_reduce path.

**Bucket:** `models`  
**Source:** [`skills/models/tt-multichip-ccl-review/SKILL.md`](../../skills/models/tt-multichip-ccl-review/SKILL.md)

## Pinning

```yaml
skills:
  - tenstorrent/skills/tt-multichip-ccl-review@<sha>
```

Pins resolve by skill name, not path.

## Provenance

See [SOURCES.md](../../SOURCES.md) for upstream paths and author attribution.
