---
"tt-project": patch
---

`tt-project`:

- ttp doctor warns when a push_checks path matches no file on the push branch
- doctor push_checks path check reads the remote ref first, follows cd, strips pytest param ids
- harness/schedules.json keeps schedule changes in git
- log a broken schedules.json once per distinct problem
- the long-tick watchdog test counts the schedules sync step
- delivery.code_tasks_may_push lets code tasks land with ttp push
- delivery.code_tasks_may_push turns on only on the user's word
