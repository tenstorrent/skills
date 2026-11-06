---
"tt-project": patch
---

`tt-project`: the context block's data-line neutraliser also catches a spoofed user's-words label in the plural possessive ("Users' words (verbatim)") and with fullwidth or other Unicode look-alike letters (for example a fullwidth "U"). It now NFKC-normalises the text before matching, so such look-alikes fall to their plain form, and the label pattern allows the apostrophe after a plural "s". A record carrying any of these can no longer pass as the user's order.
