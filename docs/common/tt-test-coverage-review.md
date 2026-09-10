# tt-test-coverage-review

Reviews test coverage for Tenstorrent changes — PCC bars and when a lower one is justified, tile-boundary and padding cases, sharding and data-format variants, program-cache tests, and a regression test on every bug fix. Use when a change alters op or kernel behaviour, whether or not it touches tests.

**Bucket:** `common`  
**Source:** [`skills/common/tt-test-coverage-review/SKILL.md`](../../skills/common/tt-test-coverage-review/SKILL.md)

## Pinning

```yaml
skills:
  - tenstorrent/skills/tt-test-coverage-review@<sha>
```

Pins resolve by skill name, not path.

## Provenance

See [NOTICE](../../NOTICE) for upstream paths and author attribution.
