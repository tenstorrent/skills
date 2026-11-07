---
"tt-project": patch
---

`tt-project`: the worker Bash credential guard no longer refuses names that only start with a credential store's (`find . -name '*.pypirc.example'`, `cat .netrc.sample`), nor a store folder's name that its path puts outside HOME (`git add .docker` or `docker build repo/.docker` in a repository). A bare store folder still counts wherever its path may lead to HOME: `~/`, `$HOME/`, `${HOME}/`, the home path itself, a relative path from HOME or after a `cd`, `pushd`, `tar -C ~`, `env -C ~` or `sudo -i` that may go there, a path through a symlink to HOME, and any path the shell expands some other way (another variable, a substitution, a brace, `host:` on a remote). `.pypirc`, `.netrc` and the other stores stay refused by name everywhere.
