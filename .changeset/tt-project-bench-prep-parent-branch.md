---
"tt-project": patch
---

`tt-project`: `ttp bench prep` no longer builds replay cells that fail in every arm by construction. A task whose spec says its branch starts from another task's branch is now replayed from that parent's recorded head (the head the spec names, else the parent's latest push queue head) to its own branch head, so its workspace holds the parent's work and its held-out tests are only its own. When the repository lacks that head or the head is not under the task's branch, the task is excluded and the manifest gives the reason. Kept tasks record the parent in the manifest.
