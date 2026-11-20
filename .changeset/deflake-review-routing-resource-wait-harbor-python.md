---
"tt-project": patch
---

`tt-project`: small reviews (`review_small`) now run on their tier's core model by default instead of the chat model; `route.classes` can still send them elsewhere. Throughput's resource lanes charge a re-queue wait to a resource only from the latest of its first hold record, the last `start_when` gate that passed and the ready check: the gate's part counts as deferral and the rest as retry wait, so one late hold no longer charges the whole wait to the resource. The Terminal-Bench (Harbor) install builds its venv on a Python 3.12 or newer from PATH, else with `uv venv --python 3.12` with uv's interpreters and cache under the bench folder, else fails naming the version needed; a stale install-failure marker no longer blocks the next try. Two flaky runtime tests now use a test clock and a fake idle check host instead of the real machine's time and load.
