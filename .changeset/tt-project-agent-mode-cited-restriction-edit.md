---
"tt-project": patch
---

`tt-project`: with `coordinator.agent_orders_as_user` on, a `charter_update` that replaces or removes a Restrictions item (by `quote`, by `replaces`, or in a dated Restrictions section) now always needs the owner's own verbatim words of the turn, whatever the new wording. Before, the owner's agent's context counted unless the edit was judged to loosen the item, so a replacement that kept every word and only added a scope ("on weekdays") went through on the agent's words. The refusal names the fix: resend with a `source` quote from the owner's own words, or add the tighter rule as a new item. The agent's context can still add new Restrictions items. With the setting off nothing changes.
