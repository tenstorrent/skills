---
"tt-project": patch
---

`tt-project`: the daemon starts fewer git processes. Its tick loop asked git for the same heads, trees, merge bases and full-id objects every tick (git was 60-75% of its 54-160 process starts a minute on live projects). Within the tick loop, a git read whose answer depends only on full object ids or on ref values (read from the ref files, not from git) is now answered from memory while those refs are unchanged, and runs git again once one moves. Worktree, index and other volatile reads still run git every time. Only answers up to 256 KB are kept, 32 MB in all. The 10-minute `spawns:` log line also says how many git reads were answered from memory. Tests that stub the daemon's nap also stub its idle nap.
