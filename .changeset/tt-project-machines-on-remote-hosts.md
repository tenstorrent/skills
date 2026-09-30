---
"tt-project": patch
---

`tt-project`: projects on other machines (`ttp new --host`) see the user's machines list.

- Their daemon reads the list on that machine, so `ttp new --host`, `ttp upgrade` of such a project,
  `ttp machines add/remove` and the new `ttp machines push [--host H]` copy it there.
- The copy is merged alias by alias with the list already there: the newest change wins, a removal
  included; on a tie the machine is kept. A newer list edited on that machine is never overwritten.
  A list there that is not valid JSON is left alone. The file stays mode 0600, and the write is
  refused and retried if the list there changed during the copy.
- `ttp setup` copies nothing: it has no project and also runs on the remote machine itself.
