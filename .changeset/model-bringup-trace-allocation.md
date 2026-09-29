---
"tt-model-bringup": patch
---

Require representative trace-allocation validation during decoder, full-model,
optimization, and serving bring-up, with stage-review enforcement. Point to the
tt-metal TraceCorrectness guide and the merged ttnn.tools.trace_allocation_tracker
APIs, including program-cache coverage and explicit corruptible-buffer invariants.
Require generator-wide prepare-before-capture warmup and trace reuse across
supported requests. Keep acknowledgments specific to reviewed backing buffers,
reject capture-wide exemptions, and require cross-trace lifetime evidence.
Use acknowledge_corruptible whenever possible. Use corruptible_allocation_scope
only when necessary, with the narrowest possible scope around reviewed allocations.
Require mixed-shape serving evidence for supported chunked-prefill paths.
Reject trace invalidation, retirement, eviction, or recapture during normal serving.
Treat correctness-driven invalidation as a design defect. Keep explicit model/device
lifecycle transitions separate from request handling. Require trace-event evidence
per execution signature, including traces captured before the mixed-shape workload.
Keep the process self-contained and the lifetime rule in the tracing skill.
Remove historical issue references and legacy API handling from operational guidance.
