---
"tt-project": patch
---

A `ttp checks` run still waiting for its host checks slot or test slots no longer keeps its repository's checks lane or the project's checks cap from the next waiter: it takes a running place only once it has its grants, and if the repository's lanes or the project's cap are full of running suites then, it hands its grants back and waits for one to end, so running suites never pass either bound.
