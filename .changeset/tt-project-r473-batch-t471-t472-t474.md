---
"tt-project": patch
---

`tt-project`:

- `ttp upgrade` restores template files a crash left empty
- `ttp setup` syncs the copied lib to disk before switching lib/current
- `ttp upgrade` never commits runtime files a crash deleted or cut short
