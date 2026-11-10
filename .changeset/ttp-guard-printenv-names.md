---
"tt-project": patch
---

A run reading outside text may `printenv` variables it names one by one (`printenv TTP_RUN_DIR TTP_PROJECT_ROOT`); a bare `printenv`, one with only options, a name that is not a plain variable name, or a credential-like name (token, key, secret, auth, password) is still refused as an environment dump.
