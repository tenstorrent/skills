---
"tt-project": patch
---

gh writes stay behind the harness gh wrapper on every provider: the runner holds the gh token variables and GH_CONFIG_DIR from the agent (an empty run-owned GH_CONFIG_DIR; only the wrapper restores them, and git's github.com credential helper goes through it); the tool hook applies the wrapper's PR body rule (prguard.form_problem) before a gh pr create/edit or gh api command runs; loopproxy.sh and a run-owned ZDOTDIR put the wrapper's folder first on PATH again; a run whose first gh is not the wrapper is marked and its gh writes are refused. A token in the system keyring is not held (ghhold.py).
