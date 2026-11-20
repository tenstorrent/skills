---
"tt-project": patch
---

`tt-project`: `ttp landings` splits each landed change's time from creation to landing by cause (queue, run, review, checks, push queue or batch, and the gaps between runs), model-free from existing records. `ttp status` and the web app show each open task's current step and how long it has waited there. The daily review lists the 5 slowest landings and the top recurring cause, and queues one fix task for it. An append-only ETA ledger (`etaledger.py`) records every ETA shown and its delivered time, backfills promises from obligation records, prints a weekly calibration line (`ttp eta --calibration`, daily review) and feeds a bounded per-kind correction factor (config `eta`, off-switch `correction: false`) into the Expected finish shown.
