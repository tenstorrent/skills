---
"tt-project": patch
---

`tt-project`: the charter gate now takes a `charter_update` in any turn when its `source` `{msg, quote}` cites a logged user message and quotes it word for word. The gate checks the quote against that message. It must come from the user's own words, or, with `coordinator.agent_orders_as_user` on, from the owner's agent's order words. It is never taken from relayer context, findings or worker notes. A mismatched quote, or one that names no logged user message, is still refused. A Restrictions change resting on such a quote that cannot be applied outside the user's turn (removing or replacing a cited item) is no longer dropped. The user's yes is recorded for that exact change and a `charter_retry` event raises it on the next turn, where the same resend applies. A quote older than `coordinator.charter_approval_days`, or a temporary change with an end date, is not queued: the refusal says to ask the user.

With agent mode on, a cited Restrictions change in a turn of the owner's agent's findings is queued the same way, not dropped. Known gap: in such a findings turn the change still counts as the agent's, so a lift of a `relay.HELD` class item (merges, posts, credentials, ...) is refused even when its `source` quotes the user's own words; the user's own words in their own turn still apply it.
