---
"tt-project": patch
---

`tt-project`:

- blind reviews and graders never see the reviewed run's model, provider, effort or family (upstream note 94c1a7841cba8cbb)
- blind review packet files written durably
- a second-reviewer device review is not held as a duplicate and daemon device reviews stay off the new-task cap (upstream note 0d30b799dc7e85f8)
