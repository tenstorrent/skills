---
"tt-project": patch
---

`ttp push --own` checks take their remote's own-branch push lane by default (one priority slot and
reserved test lane, one push at a time), so they never wait behind long `ttp checks` runs in the host
cap or their project's test budget; `delivery.own_push_reserved_lane` false restores the ordinary turn.
Push checks wait on the host load guard at most `checks.push_load_wait_s` (default 300 s; `off`: no
bound), then start on their branch's reserved lane alone. Task checks keep the load guard as before.
