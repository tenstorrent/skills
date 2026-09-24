---
"tt-model-bringup": patch
---

Require representative trace-allocation validation during decoder, full-model,
optimization, and serving bring-up, with stage-review enforcement. Point to the
tt-metal TraceCorrectness guide and the merged ttnn.tools.trace_allocation_tracker
APIs, including program-cache coverage and explicit corruptible-buffer invariants.
Require generator-wide prepare-before-capture warmup and trace reuse across
compatible requests. Keep acknowledgments specific to reviewed backing buffers,
reject capture-wide exemptions, and require cross-trace lifetime and negative-control
evidence when acknowledgments change.
