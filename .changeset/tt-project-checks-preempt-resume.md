---
"tt-project": patch
---

`tt-project`: a `ttp checks` run preempted by an urgent run now resumes each plain local pytest check from the test files it passed on the same tree and env (the push queue's resume plugin), instead of rerunning them from zero; a changed tree or env, and any run that was not preempted, runs everything. A run that cannot resume (another runner, pytest-xdist, a remote check) is no longer preempted once its log shows it 90% or more through its tests.
