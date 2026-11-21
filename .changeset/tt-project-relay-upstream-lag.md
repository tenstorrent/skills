---
"tt-project": patch
---

`delivery.push_upstream`: a model-free check of how far a relay push remote is ahead of the real upstream,
shown in `ttp status`, the daily review and `ttp push audit`, with one alert past `relay.gone_after_s`
(unknown, never 0, when the upstream cannot be read); landing replies and post_land events say when a
landing is on the relay only; `ttp pause --resource push` without `--end-when` warns.
