---
"tt-project": patch
---

`tt-project`: before the push queue replays an approved head, it leaves out each of the head's commits that is on the target already: named by a `Ttp-Replayed-From` trailer there (a settled replay or a squash that carries it) or the same patch (`git patch-id --stable`), also when an earlier entry of the same batch carries it. A branch built on another task's commits that have since landed in another form no longer reports a conflict, so its approval is kept and no review goes back; a conflict in the head's own commits is still one. A head whose commits are all there is recorded as landed with the commit that carried it, so `ttp landed --task` passes for it.
