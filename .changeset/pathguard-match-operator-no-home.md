---
"tt-project": patch
---

`tt-project`: the write guard no longer reads a regex match operator (`[[ $x =~ re ]]`, perl's `=~` and `!~`) in a script as a home folder reference. With a `../<name>` path elsewhere in the script, that made it name a protected `~/<name>` and refuse a script that only wrote its own output files. A `~` that names home (`T=~`, `~/x`, `cd ~`) still counts, and a protected folder still covers only itself and what is under it.
