---
"tt-project": patch
---

`tt-project`: the outside-writes scan finds the command past `timeout [options] <duration>`, past long sudo, env, xargs, time and nice options that take a value (`sudo --user root rg ...`), and inside `<(...)` and `>(...)`; it reads `git [options] grep` as grep. Their pattern text is no longer read as paths, so `timeout 30 grep pat ~/x`, `git grep 'rm /tmp/x'` and `diff <(grep /x f) y` name no outside write.
