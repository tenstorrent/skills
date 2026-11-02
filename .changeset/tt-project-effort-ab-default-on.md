---
"tt-project": patch
---

`tt-project`: the effort A/B of standard-tier code tasks (high vs medium effort) now runs by default (`effort_ab.enabled` defaults to true); a project that set it to false keeps false. The daemon applies the end rule every 6 hours: once each arm has 60+ landed changes, it records the winner (`effort_ab.winner`, `decided_at`), turns the split off and sends one low alert; a medium winner then runs every code task the A/B covered at medium effort (Jev's pick, if switched on, takes precedence). `ttp stats --effort-ab` adds per-arm thinking-token totals from the result usage (Claude's `output_tokens_details.thinking_tokens`, Codex's `reasoning_output_tokens`), or `n/a` when the provider gives none, beside $ per landed change.
