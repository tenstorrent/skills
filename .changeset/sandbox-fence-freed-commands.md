---
"tt-project": patch
---

Sandbox: a project frees its own host-reaching commands (an executable in its harness `bin/`, such as a
script that wraps ssh) with the new config key `sandbox.fence_freed_commands` (`["my-ssh"]`, or
`{"command": "my-ssh", "env": ["VAR"]}`), instead of editing FENCE_FREED in hook.py and FENCE_EXCLUDED in
providers/claude.py. The Claude sandbox then runs `my-ssh ...` (also after `timeout N`) outside the write
fence as a simple command, and the hook refuses it beside `&&`, `;`, a pipe, a file redirect or a
substitution, as it does ssh. Names outside the harness `bin/`, shells, wrappers and `ttp`, `git`, `gh`
are left out with one daemon log line. Upgrades no longer conflict on those template lines; the upgrade
merge task names the key. The web token check is unchanged and still refuses a freed command naming it.
