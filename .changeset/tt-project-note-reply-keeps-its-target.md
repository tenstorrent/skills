---
"tt-project": patch
---

`tt-project`: a `ttp note --reply <fp>` now keeps the note it answers all the way to delivery. The reply's text starts with `[reply to note <fp>]`, and its queued, outbox and inbox rows carry `reply_to`, so its id includes the note it answers. Before, the id hashed only the recipient and the text, so the same words sent as replies to two different notes got one id: the second was dropped as a duplicate, and nothing linked either reply to its note. Now each reply is delivered separately, linked to its own note, and the CLI prints its id. The same words to the same note still deduplicate. Plain `ttp note --to` notes keep their old ids. Rows queued by an older version still go out. Older peers accept the marked reply, because their receive check hashes the text, which now holds the mark.
