---
"tt-project": patch
---

The run-mark guard reads a `#` as a comment only where it starts a word, as the shell does, so `rm a#b <record>` is refused instead of passing. A heredoc body that only cat or tee writes to another file (quoted delimiter, nothing piped on, and every other command in the line only prints or reads) is read as data, so a prose line naming the record in it no longer stops a worker; a body run by a shell or interpreter, piped on, or written to the record itself is still refused. Whether a word before the record in a prose clause is a command now comes from a fixed list of commands that change or create files instead of a PATH lookup, so the same sentence gets the same verdict on every host.

When the guard cannot tell where a comment ends, it refuses a command that names the record instead of guessing: a comment ending in a backslash beside a heredoc (bash ends the comment at its newline, so the next line runs), or a comment in a command that also has a `${...}` expansion (a `#` inside one is no comment). So `: ${x:- #} ; rm <record>` and a comment that ends in a backslash followed by `rm <record>` on the next line are refused.

A comment starts wherever the shell starts a new word, now also right after a `)`, so `(true)# note` and its quote can no longer hide the command on the next line; after the `)` of a `$()`, or after a redirect operator the `#` is part of a word and starts no comment. The comment-start rule lives in one place, the shell-quote walk, which the record guard now reads instead of keeping its own copy.
