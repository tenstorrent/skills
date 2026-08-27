# tt-trace-review

Reviews trace capture and replay safety — nothing host-side inside capture, program-cache warmup with exact signatures including scalar argument values, no allocation after capture, and device-owned autoregressive state. Use when reviewing changes touching begin_trace_capture, execute_trace, generator decode paths, or program-cache warmup.

**Bucket:** `models`  
**Source:** [`skills/models/tt-trace-review/SKILL.md`](../../skills/models/tt-trace-review/SKILL.md)

## Pinning

```yaml
skills:
  - tenstorrent/skills/tt-trace-review@<sha>
```

Pins resolve by skill name, not path.

## Provenance

See [SOURCES.md](../../SOURCES.md) for upstream paths and author attribution.
