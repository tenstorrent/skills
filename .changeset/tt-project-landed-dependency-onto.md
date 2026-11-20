---
"tt-project": patch
---

`tt-project`: a branch built on another task's approved head lands without a push conflict once that task landed rebased or reworked: the push queue leaves out the stale copies of its commits through the landed-task mapping (approved head to landed commit), and `ttp push` rebases with `--onto <branch tip> <that head>`. A conflict in the branch's own commits is still reported as before.
