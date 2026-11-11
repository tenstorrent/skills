---
"tt-project": patch
---

`tt-project`: `ttp machines change close <id>` now answers each daemon page about that change, which another project on this machine sent, with a fixed close report marked as confirming the page. The paging project settles that reply model-free, with no coordinator wake. It is recorded with its reason and note id, kept as a handled event in the feed, and counted in the daily review. The settle rests on structure only: the reply answers the project's own page, it comes from the paged project, the ledger shows the change closed by that project (or the reply carries the confirm marker), its text is exactly the close report, and its severity is low or normal. A reply about a change that is still open, one with any other words, a high one, or a reply to a note that is not a page still wakes the coordinator. The page text now says that the close answers the page.
