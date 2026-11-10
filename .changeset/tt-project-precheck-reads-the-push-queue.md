---
"tt-project": patch
---

`tt-project`:

- a review precheck reads the push queue by head: when the queue already holds (approved, batched, held) or pushed exactly that head, the daemon starts no second full `ttp checks` on it. Its precheck folder records the queue's verdict, which `ttp checks --result` prints (exit 0 once pushed or landed, 75 while the queue holds it). A precheck still waiting for a slot ends once its review approves the head, as its batch runs the checks on the commit it pushes; tests
