---
"tt-project": patch
---

`tt-project`: the worker prompt says detached driver chains fail fast (`set -eo pipefail`, no piping
into tail/head, per-step exit codes, first failure in the marker, timeouts sized up front). The harness
skill says watcher scripts that workers also run take a non-blocking lock, re-check, and dedupe by key.
