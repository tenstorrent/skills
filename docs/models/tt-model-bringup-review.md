# tt-model-bringup-review

Reviews TTNN model code — layout and memory-config defaults, the decode residual contract, composite-op preference, program-config pitfalls, hidden host fallbacks, and logical batch versus tile padding. Use when reviewing model bringup or optimization changes under models/ or ttnn/ Python.

**Bucket:** `models`  
**Source:** [`skills/models/tt-model-bringup-review/SKILL.md`](../../skills/models/tt-model-bringup-review/SKILL.md)

## Pinning

```yaml
skills:
  - tenstorrent/skills/tt-model-bringup-review@<sha>
```

Pins resolve by skill name, not path.

## Provenance

See [NOTICE](../../NOTICE) for upstream paths and author attribution.
