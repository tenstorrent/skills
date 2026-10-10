---
name: model-op-analysis
description: Route a generic tt-model-op-analysis plugin request to either static-op-analysis or measured-op-analysis. Use when the user names or invokes the plugin without explicitly choosing one of its two analysis skills.
---

# Model op analysis router

This plugin has two independent workflows. Do not infer one from open files, IDE state, a test
command, available hardware, or the plugin's default prompt.

Ask the user which workflow to run, briefly distinguishing them:

- `static-op-analysis`: inspect pinned source and build validated CSV op tables; no device run.
- `measured-op-analysis`: run the test under Tracy and report observed timing and footprint.

Stop until the user explicitly chooses one. After the choice, invoke only that skill and follow
its interactive setup one question at a time. Never batch the model, test, implementation,
source ref, targets, or output location into one confirmation. Do not begin source inspection,
device checks, output creation, or delegation while waiting for the workflow choice.
