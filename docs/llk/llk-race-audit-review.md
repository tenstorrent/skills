# llk-race-audit-review

Reviews tt-llk kernel changes for the nine race hazard classes — cfg-word overlap, dataflow CB sync, instruction latency, mailbox sync, MMIO ordering, NoC sync, reconfig stalls, semaphore handshakes, and srcreg bank sync — plus the cross-class seams between them. Use when reviewing changes under tt_metal/tt-llk.

**Bucket:** `llk`  
**Source:** [`skills/llk/llk-race-audit-review/SKILL.md`](../../skills/llk/llk-race-audit-review/SKILL.md)

## Pinning

```yaml
skills:
  - tenstorrent/skills/llk-race-audit-review@<sha>
```

Pins resolve by skill name, not path.

## Provenance

See [NOTICE](../../NOTICE) for upstream paths and author attribution.
