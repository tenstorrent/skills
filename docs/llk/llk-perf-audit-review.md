# llk-perf-audit-review

Static performance review of Tensix compute kernels — wasted cycles, redundant register and memory traffic, loop-invariant work, and cheaper numerically-equivalent instruction sequences. Gated by a provenance lens (sfpi-compiled versus hand-written) and a semantic-equivalence test. Use when reviewing SFPU or Tensix kernel changes under tt_metal/tt-llk.

**Bucket:** `llk`  
**Source:** [`skills/llk/llk-perf-audit-review/SKILL.md`](../../skills/llk/llk-perf-audit-review/SKILL.md)

## Pinning

```yaml
skills:
  - tenstorrent/skills/llk-perf-audit-review@<sha>
```

Pins resolve by skill name, not path.

## Provenance

See [NOTICE](../../NOTICE) for upstream paths and author attribution.
