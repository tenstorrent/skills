---
"tt-model-bringup": minor
---

Add Stage 0 to inventory model layer types and prepare batch-one PCC tests with
cached real-weight references from required ISL/OSL workloads. Later stages reuse
the baseline and require evidence before correcting tests. Preserve stage numbers
1–11 and support starting and resuming Stage 0. Require real-text prefill/decode
inputs, real populated KV caches and separate output and final K/V PCC checks.
Leave lightweight model analysis and semantic test contracts; Stage 1 owns decoder
structure and its backend adapter. Validate fixture extraction with thin wrappers
around original PyTorch layers, without a decoder skeleton or handwritten reference.
Run CPU-adapter and sensitivity checks at Stage 0, then enforce complete real-input
output/state PCC evidence by executing manifest-selected pytest cases after every
stage. Record fresh results and source/artifact hashes before existing stage gates.
