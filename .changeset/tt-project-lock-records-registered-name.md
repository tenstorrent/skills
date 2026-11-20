---
"tt-project": patch
---

`tt-project`: `ttp lock` holder and shared-hold records name the project as registered (the name `ttp list` shows), not its harness folder's name (always `tt-project`), so lock-wait and lock-cycle alerts no longer pin another project's holders on whichever project reads them. Older records resolve through the registry by their project root or lock folder, else read "unknown project at <root>"; a detached job's leftover slot is freed only by the project whose root its record names.
