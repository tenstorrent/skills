---
"tt-project": patch
---

The push queue sends an approval alone as the failing tip's fix only when its change touches the failing tests, or its spec names the tip or a failing test as well as saying it fixes them. That lone batch's checks also run the failing tests by id. While the push queue is off, a review that pushed its head itself is seen as landed even though `ttp push` rebased it to another sha. The guide says how waiting `ttp push` runs take turns.
