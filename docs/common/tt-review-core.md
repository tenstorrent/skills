# tt-review-core

Shared review contract for all Tenstorrent code review — severity vocabulary, the evidence rule, scope, output shape, and the do-not-flag guards. Use when reviewing any change to a Tenstorrent repository (tt-metal, tt-llk, tt-inference-server, model code), and load it before any domain review skill.

**Bucket:** `common`  
**Source:** [`skills/common/tt-review-core/SKILL.md`](../../skills/common/tt-review-core/SKILL.md)

## Pinning

```yaml
skills:
  - tenstorrent/skills/tt-review-core@<sha>
```

Pins resolve by skill name, not path.

## Provenance

See [SOURCES.md](../../SOURCES.md) for upstream paths and author attribution.
