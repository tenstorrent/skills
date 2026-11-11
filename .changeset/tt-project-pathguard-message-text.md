---
"tt-project": patch
---

`tt-project`: the credential folder guard treats the text of a `ttp note`, `notify` or `say` message as data: a message that only names a credential path passes, also with interpreter code elsewhere in the same line. Command substitutions (`$(...)` and backticks, also inside double quotes) are now read like any command, so one that writes a credential folder is refused, and one inside a message that names a credential path is refused even to read, since the message would send what it prints. A `ttp note` line inside a heredoc body is a program's input, not a shell command, so its text stays in the code scan.
