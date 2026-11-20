---
"tt-project": patch
---

`tt-project`: a land or review task inside the worker sandbox can now land on a push branch that the project's main checkout has checked out. The sandbox writes the project's `.git` but not the root's working tree, so `ttp land` failed there with a permission error. It now hands that fast-forward to the running daemon, which does it on the host with no setup. The daemon first checks that the checkout belongs to the project's repo, is clean, and is still at the landing's base, and that the landed commit contains that base. A dirty, moved or diverged checkout is never overwritten: `ttp land` exits 6, 5 or 1 with a clear message, and nothing moves.
