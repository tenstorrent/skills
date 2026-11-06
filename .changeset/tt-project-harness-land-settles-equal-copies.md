---
"tt-project": patch
---

`tt-project`: landing a harness task's branch no longer stalls when the harness checkout holds uncommitted copies (changed or untracked) of files the branch changed whose content equals the branch's head version or an earlier one since the merge base. Each such copy is backed up to `state/harness-land-backups/<date>-t<id>/`, put back to HEAD, and the merge goes ahead; the daemon log names the files and the backup. Other uncommitted files stay untouched; a merge still refused puts the copies back and its report names them and the backup.
