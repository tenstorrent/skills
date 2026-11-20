---
"tt-project": patch
---

`tt-project`:

- push: a model-free public scan (`delivery.public_scan_paths`, repo globs, off by default) in `ttp push` and the push queue refuses an added line under those paths that names a local name only this host's setup knows (machine aliases, their hostnames and domains, registered project names, the login, git identity, remote hosts, chat ids) unless the repository already names it elsewhere, or that cites a chat message or task id (`msg #<digits>`, `task #<digits>`; in Python only in comments and docstrings)
- workstation gh route: `gh repo fork <owner>/<repo> --fork-name <name> --clone=false` (no other flag) is allowed, refused when the workstation's gh user already has a repo by that name
- setup-box: the Slurm cluster route covers 1-chip, 4-chip and whole-node jobs on one page; generic examples and documentation IP ranges in docs and tests
- README: the install line takes the plugin from the default branch
