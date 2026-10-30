# Charter guard

The coordinator's `charter_update` (runtime `coordinator.py`, `_reject_contradicting_append`) refuses added text that a standing Restrictions item would contradict, quoting the item and naming the forms that pass: a quoted resend, `both_hold` plus a companion edit for an exception, or `both_hold` (or the text saying it loosens no restriction) when both hold.
A companion edit keeps the quoted item word for word and only adds a clause naming, by id, an exception that stands in a temporary or dated Restrictions section, with no object or action of its own, on the sentence that exception is about.
The user's yes to that exception covers it in a later turn and it is checked against its own item only.
Removing or really loosening an item still needs the user's word or `over`.
The refusal says a user's loosening goes in as written (a quoted resend on their word, no `over` and no end they did not give), and names the temporary-exception route only when the user's own words make it temporary.
After such a loosening or removal (`quote`, or `replaces` of a Restrictions section, judged item by item), or an `over` retirement, a `restriction_tools` event (runtime `restriction_audit.py`) names the tool files still carrying the old item's terms; it never edits them.
A refusal that names such a fix raises an immediate `charter_retry` event, at most two per change.
A Restrictions item binds only when it cites its source (runtime `restriction_source.py`): a user message id ("(user msg #N)", "user messages #N, #M"), "(origin: Brief)" or "(origin: harness gate)"; a nested bullet or continuation line counts as part of its item, a sub-heading is an item of its own.
New or changed Restrictions text needs `source`: `{msg, quote}` with the quote in the user's own words of that message (never the relayer's context), `{origin: brief, quote}` with the quote in the Brief, or `{origin: gate}`, sent as that object's JSON text (the action schema types `source` as a string; an object works too); each refusal names the check that failed (missing, not JSON, origin, message, quote); the cite is added to each item that has none, and a cite in the text that the source does not name is refused. A charter change the user said yes to is cited with that message.
An item with no cite is advisory: prompts list it apart from the binding block (workers never get it as binding), the coordinator removes it without the user's word or `over` (a whole section only when every item in it is unsourced), and an ask about it alone is refused with the drop instruction.
Charters written before this rule keep their uncited items as advisory until the coordinator resends them with `source`.
Raise `CHARTER_GUARD_VERSION` and its pin when the guard changes.
