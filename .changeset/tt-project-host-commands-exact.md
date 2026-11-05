---
"tt-project": patch
---

`tt-project`: a `runner.host_commands` entry can now be `{"cmd": "<command>", "exact": true}`, which `ttp host-run` runs only for a request of exactly its words, so a worker cannot append words to a privileged command and have the daemon run them as root. Plain string entries keep matching as a prefix. An `exact` that is not true or false is refused, named, like any malformed entry, and `ttp doctor` warns on a plain (prefix) entry that starts with sudo.
