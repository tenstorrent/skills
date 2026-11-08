---
"tt-project": patch
---

`tt-project`: every check run (`ttp checks`, `ttp push`, the push queue) now gets a throwaway HOME, on this machine and on check, spill and Slurm hosts, so a project's tests never read or write the account's home folder. The XDG config, data and state folders are inside it; the cache stays shared so runs stay fast, and toolchains under HOME (rustup, cargo, pyenv, go, nvm, the Hugging Face cache) keep their real folder. `checks.home_files` lists the files copied in (default `.gitconfig`, from the same host only); credentials, ssh and gh config are refused. A project whose checks need the real HOME sets `checks.scratch_home false`.
