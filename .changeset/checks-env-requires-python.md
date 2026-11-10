---
"tt-project": patch
---

tt-project: the checks' pinned Python env is made from a Python its lock supports. Where ttp ran under
macOS's own /usr/bin/python3 (3.9), the default lock's env failed to build before any check ran (pip found no
iniconfig 2.3.1, which needs Python 3.10), and `ttp checks` waited on a host fault. A lock may now name its
Pythons with a `# requires-python: >=3.10` line (the default lock does): when the Python running ttp is older,
the env is made from the first Python on PATH or in the usual macOS and Linux install folders that meets it, and
the env's key names that Python. When none does, nothing runs and the error names the Python needed and the one
found. The line is left out of the key, so hosts whose envs already work keep them.
