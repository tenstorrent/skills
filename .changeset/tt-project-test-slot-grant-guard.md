---
"tt-project": patch
---

`tt-project`: a test or build slot grant's release and a waiter's count-and-take are serialized by a guard file, so a waiter that polls while another run is freeing its slots no longer takes part of that grant while a later waiter takes the rest; each waiter gets its whole grant, in arrival order.
