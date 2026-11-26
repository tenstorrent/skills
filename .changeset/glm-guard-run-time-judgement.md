---
"tt-project": patch
---

GLM guard: fewer false refusals under the harm bar. A variable set to a literal after simple commands, a read of the
run's own `$TMPDIR` or `$TTP_RUN_DIR`, a quoted `read` or `eval` inside a `python3 -c` string, `git -c commit.gpgsign=false`
(or `user.name`, `user.email`) `commit` and `git grep` with shell characters in the pattern are no longer refused up
front; credential reads, other-project edits, hidden home entries and `git -c` for any other key stay refused. An `ls` or
`stat` of a credential folder no longer raises a high outside-writes event; reading a file in it still does.
