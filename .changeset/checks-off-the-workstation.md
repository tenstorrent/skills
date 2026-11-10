---
"tt-project": patch
---

tt-project: a `ttp checks` run or push batch whose suite is pinned to a check host ("host") now spills its other heavy checks to a spill host at once instead of queuing for this machine's host-wide checks slot (checkspill.spillable counted a host-pinned check as unspillable); a detached run re-executed by an upgrade is no longer kept off every spill host for good; the push train runs a light local check (claude plugin validate) without the host-wide slot, as push.run_slot does; the load guard and the live-cores gate count CPU demand only once it lasted checks.load_sustain_s (default 60 s, readings shared in check-slots/cpu-demand.jsonl), so a burst of short-lived git, ssh or python processes holds nothing, and macOS memory pressure at warn or critical holds local suites
