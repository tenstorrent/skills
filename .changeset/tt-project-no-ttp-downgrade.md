---
"tt-project": patch
---

`tt-project`: An older plugin can no longer downgrade an installed `ttp`. `ttp setup` refuses when
`~/.tt-project/lib/current` holds a newer version (`--force` installs anyway), and creating a project
on another machine keeps that machine's `ttp` when it is the same version or newer. While the
installed `ttp` is older than a project's harness, its daemon raises one high alert that clears by
itself once a newer `ttp` is installed again.
