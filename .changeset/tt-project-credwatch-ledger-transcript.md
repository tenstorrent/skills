---
"tt-project": patch
---

`tt-project`: the credential tripwire no longer raises an urgent alarm for a change another project recorded. When this machine's change ledger has an entry made while the run ran that names the changed file, the observation is normal severity and names the entry, its project and task, and the run's hand-off is not failed for it. Otherwise it stays high and quotes the first line of the run's transcript that names the file or runs a command that writes it (ssh-keygen without a scratch `-f`, `gh auth`, `git credential`); with none it says the run's commands did not show it and gives the file's time, so the search starts there.
