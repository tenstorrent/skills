---
"tt-project": patch
---

A landed-claim the local tracking ref cannot confirm is checked again after fetching the push
branch, so a landing another route pushed is never called 'not landed' on a ref that lags it, and
a hand-off the remote check confirmed keeps its claim as written.
