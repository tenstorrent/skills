---
"tt-project": patch
---

`tt-project`: project state heals itself, and every run's spend counts.

- A worker hand-off of any length is stored as valid JSON; long fields are shortened instead of
  the text being cut. The coordinator's digest reads older cut rows without failing.
- A run row left without a directory or process is reaped as lost; a run whose end cannot be
  processed no longer blocks the others or retries forever.
- A run's end, its spend and its task's new state commit in one transaction.
- A task left `running` with no live run goes back to the queue with no attempt spent. A worker
  that fails to start leaves its task queued. A coordinator turn that cannot start backs off.
- A queued task whose dependency failed, was cancelled or does not exist is blocked with the
  reason, which the coordinator sees. Follow-ups beyond the first five are listed in the task event.
- A Claude run that ends without its final report (killed, lost) is priced from its streamed
  usage, counted once per message, at the project's own observed rate; the fallback rate is
  configurable. The estimate counts toward the caps, the runaway guard and the task's budget.
- Dollar caps apply to the project total across providers; plan windows stay per provider.
- A Claude run counts as logged out only from its stderr, its result's error or an
  `authentication_failed` event, so a killed worker whose last message mentions a 401 no longer
  pauses the provider.
