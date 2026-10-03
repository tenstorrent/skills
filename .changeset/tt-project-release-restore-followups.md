---
"tt-project": patch
---

`tt-project`: `ttp setup --force` writes its downgrade marker before it switches `lib/current`, so a
daemon check in between no longer undoes a deliberate downgrade. When the daemon points
`lib/current` back at the harness's release, status and the web app show that release at once
instead of up to an hour later. The low notice that it did so is sent at most once a day.
