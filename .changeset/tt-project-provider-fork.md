---
"tt-project": patch
---

Workers and reviews start from a fork of the coordinator's session on more providers: the chat provider (Claude Code through its bridge) forks a Claude Code coordinator's transcript, and Codex forks a Codex coordinator's rollout (a copy under a new id that `codex exec resume` continues). Cursor keeps the disk context block (its CLI has no fork and its chat store is undocumented). Each run's `coord_context` note now also records `from` (the provider whose session it copied) and `resume` for a run continuing its own session, so the fork rate counts per provider.

`Provider.start_from_session` is now the one call that starts a run from a saved session on any provider, and `Provider.fork_gap` reports per provider whether it forks or why it takes the disk block. Codex workers' sub-agents (the `multi_agent` feature, on in `codex exec`) now count in the run's cost and the runner's budget: each writes its own rollout outside `turn.completed`, so their tokens are added from those rollouts.
