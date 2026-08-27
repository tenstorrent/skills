# tt-precision-review

Reviews dtype and math-fidelity policy — per-tensor-group precision, the prefill versus decode KV-cache dtype asymmetry, reduced-cache candidates, and PCC-collapse triage. Use when reviewing changes to dtypes, math fidelity, compute kernel configs, or cache precision.

**Bucket:** `models`  
**Source:** [`skills/models/tt-precision-review/SKILL.md`](../../skills/models/tt-precision-review/SKILL.md)

## Pinning

```yaml
skills:
  - tenstorrent/skills/tt-precision-review@<sha>
```

Pins resolve by skill name, not path.

## Provenance

See [SOURCES.md](../../SOURCES.md) for upstream paths and author attribution.
