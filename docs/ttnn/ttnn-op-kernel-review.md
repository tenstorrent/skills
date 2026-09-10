# ttnn-op-kernel-review

Structural correctness review for TTNN op kernels — init and data-format reconfig, TRISC synchronization, the tile_regs protocol, circular-buffer ownership and UB, work distribution, semaphores, control-flow CB balance, and in-place misuse. Use when reviewing changes to reader/compute/writer kernels, program factories, or program descriptors under ttnn/ or tt_metal/.

**Bucket:** `ttnn`  
**Source:** [`skills/ttnn/ttnn-op-kernel-review/SKILL.md`](../../skills/ttnn/ttnn-op-kernel-review/SKILL.md)

## Pinning

```yaml
skills:
  - tenstorrent/skills/ttnn-op-kernel-review@<sha>
```

Pins resolve by skill name, not path.

## Provenance

See [NOTICE](../../NOTICE) for upstream paths and author attribution.
