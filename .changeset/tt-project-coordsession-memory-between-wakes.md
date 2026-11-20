---
"tt-project": patch
---

`tt-project`: in coordinator session mode (`coordinator.session.mode on`), a resumed wake now leads with the memory entries added, changed or retired since the session last saw them (standing memory, the STANDING block, the user's corrections and recent decisions and facts). The session state keeps a hash of each entry it was sent, so an entry recorded between a wake and a prompt re-render, which emptied the digest's own deltas, is no longer missed until the next rollover. An unchanged memory adds nothing, a line the digest already carries is not repeated, and a failed resume sends its entries again. Mode off is unchanged.
