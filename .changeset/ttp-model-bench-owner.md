---
"tt-project": patch
---

tt-project: one project per machine owns the daily chat model bench. The first eligible project by
name (live daemon, chat endpoint, not paused, no `model_bench.opt_out`) keeps a model-free command
schedule that runs `ttp model-bench run --detach` at `model_bench.bench_at` (default 02:00 home
time). Every daemon checks the lease hourly without a model call: after `poke_after_h` (48 h) with no
result it pokes the owner once, which runs the bench at once with a feed line in both projects; after
`take_over_after_h` (72 h), or when the owner is paused, opted out or down, the next project by name
takes over under a lock, with one low alert naming both.
