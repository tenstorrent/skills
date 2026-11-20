---
"tt-project": patch
---

`tt-project`: a check that fails on a shell syntax error in a script outside the change under test (such as a project's harness script rewritten in place while the check ran it) that parses again now is rerun once after a short settle. Failing so again, it is an infrastructure fault: `ttp push` exits 75 with a `retry_when`, the push queue requeues the batch (outcome `script_torn`, retried after 10 min), and `ttp checks` exits 75 with `infra_fault: script-syntax-transient`. The head is never failed for it. A syntax error in a file the change touches, or in a script still broken, still fails the check. A new project's harness files are now all renamed into place, never written in place, and the harness skill says so.
