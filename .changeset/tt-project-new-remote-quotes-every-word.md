---
"tt-project": patch
---

`ttp new --host` now quotes every word of the remote `ttp new` command. Before, a `~/` folder or launcher path was forwarded unquoted, so the remote shell split a folder with spaces or shell characters into several words. A leading `~/` is now sent as `"$HOME"/` plus the quoted rest, so it still expands to the remote home.
