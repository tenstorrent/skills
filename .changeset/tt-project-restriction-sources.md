---
"tt-project": patch
---

`tt-project`:

- A charter Restrictions item now records its source: a user message id ("(user msg #N)"), "(origin: Brief)" or "(origin: harness gate)". `charter_update` on Restrictions needs `source` (`{msg, quote}` with the quote in the user's own words, never a relayer's context; `{origin: brief, quote}`; or `{origin: gate}`), adds the cite to each item, and refuses a cite in the text that the source does not name. Without one it is rejected with a hint to record the rule with `memory_add` instead.
- An item with no cite is advisory: it is listed apart from the binding block (workers never get it as binding), the coordinator removes it without the user's word or `over`, and an ask about it alone is refused with the drop instruction. Existing charters' uncited items become advisory until the coordinator resends them with `source`.
- A sub-heading in Restrictions no longer takes the paragraph under it along when items are classified. `CHARTER_GUARD_VERSION` is 12 (source cites are ignored when comparing items, and the user's recorded yes to a charter change matches its text with or without the cite).
