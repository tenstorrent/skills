---
"tt-project": patch
---

`tt-project`: an ask for a step only the user can take (an access or funds ask, or one naming a sign-in, a device code or a credential) no longer closes as "its work landed" when a task it names lands, since that task may be only context. It closes once the step's check passes for a task it names, or a task that carries one on (`unblock.close_step_done`, from `userstep.probe_passed`), or when the user answers it, so it stays in 'Needs you' while work still waits on the step.
