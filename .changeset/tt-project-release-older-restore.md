---
"tt-project": patch
---

`tt-project`: When an older plugin's `ttp setup` replaces a newer install, each daemon now points
`~/.tt-project/lib/current` back at the newest complete release at its harness version or newer and
posts a low notice, instead of asking the user to run `ttp setup`. A downgrade made with
`ttp setup --force` is left alone. The alert remains only when nothing newer is left to restore, and
it is raised again if the problem comes back.
