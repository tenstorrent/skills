---
"tt-model-bringup": patch
"tt-review-skills": patch
---

Update generator and serving guidance for the version-1 decode reload commands.
Keep async capability separate from command negotiation. Require output buffers
to remain valid through deferred readback and plugin use of returned host views.

Preserve generator-wide preparation before trace capture. Keep internal variants
and synthetic warmup history inside the components that own them. Clarify when
warmup can preserve sampling state and when history setup must also be prepared.
