---
"tt-project": patch
---

A task run's untrusted-input record is now refused only for real effects on it: a redirect into it, a
path to it given to a command that may write, or any mention in a command that runs code, a
substitution or a heredoc. Its name inside a note's quoted text, a grep pattern, a read or a patch's
text no longer blocks a command that writes only other files.
