---
"tt-project": patch
---

Coordinator: an ask the user resolved with a clear yes now counts as their approval of the charter
change it names (the section, the `quote` or the heading `replaces` resolves to, and any new text),
within `coordinator.charter_approval_days`. A resolved ask wrote no approval record before, so the same
change resent in a later turn was refused with "No approval of the user's is on record". The approval
is recorded once and used once; only the user's own verbatim words count (never a relayer's context,
a finding or the harness's own message), a removal needs an ask to remove, and an answer that is not a
clear yes, another section or target, or an expired yes is still refused. The refusal now names what
fixes it: the user's yes in that turn, such a resolved yes ask, or `over` naming what ended the
restriction. The charter guard version goes to 11.
