---
"tt-project": patch
---

`tt-project`: The charter guard judges each clause of an appended sentence on its own, so a ban in one
clause never cancels an allowance in another ("No restriction remains and workers may push to main").
The Restrictions lint reads a ban the same way: an item like "No ticket needed and workers can push to
main." now pairs with "Never push to main.", with "can <action>" read as a permission in such a clause.
Conditional and "only" permissions still lift nothing, and "can" with no action after it stays ability.
