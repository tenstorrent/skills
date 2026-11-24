---
"tt-project": patch
---

The run-mark guard reads a `#` as a comment only where it starts a word, as the shell does, so `rm a#b <record>` is refused instead of passing. A heredoc body that only cat or tee writes to another file (quoted delimiter, nothing piped on, and every other command in the line only prints or reads) is read as data, so a prose line naming the record in it no longer stops a worker; a body run by a shell or interpreter, piped on, or written to the record itself is still refused. Whether a word before the record in a prose clause is a command now comes from a fixed list of commands that change or create files instead of a PATH lookup, so the same sentence gets the same verdict on every host.

A comment ending in a backslash no longer swallows the command on the next line (the comment is now cut before backslash-newlines are joined), and a `#` inside a `${...}` parameter expansion is read as the literal it is, not as a comment, so `: ${x:- #} ; rm <record>` is refused again.
