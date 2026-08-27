# tt-l1-memory-review

Reviews per-core L1 footprint and circular-buffer sizing — buffer inventory discipline, data-movement cost across memory tiers, CB capacity versus tile counts, and accumulator sizing. Use when reviewing program factories, CB allocation, blocking or work-split changes, or any change that adds a buffer.

**Bucket:** `metal`  
**Source:** [`skills/metal/tt-l1-memory-review/SKILL.md`](../../skills/metal/tt-l1-memory-review/SKILL.md)

## Pinning

```yaml
skills:
  - tenstorrent/skills/tt-l1-memory-review@<sha>
```

Pins resolve by skill name, not path.

## Provenance

See [SOURCES.md](../../SOURCES.md) for upstream paths and author attribution.
