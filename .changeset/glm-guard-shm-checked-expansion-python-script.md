---
"tt-project": patch
---

GLM guard: /dev/shm is scratch like /tmp (the harness's own `ttp-<uid>` folder there stays protected); `${TTP_RUN_DIR:?}` and `${HOME:?}` name their folder; the body of `cat > FILE <<'EOF'` is python code the run-time judge watches (not shell text) when the line only writes that one file and runs it with a bare `python FILE`, with no assignment, substitution, wrapper or other command, and the judge is loaded.
