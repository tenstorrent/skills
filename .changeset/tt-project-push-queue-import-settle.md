---
"tt-project": patch
---

`tt-project`: the push queue settles a Python conflict made only of import statements without a model: the result imports every name each side imports (a name one side dropped stays dropped), placed in the tip's layout, inline or one name per line. It refuses code in the conflict, star imports, `;` or `\` lines, a name bound twice and any spot it cannot place uniquely. Like the added and hunks rules, the verdict holds only when the push checks pass. The worker prompt size caps moved from shared constants to `tests/tt_project_runtime/worker_prompt_budget.txt`, where each change appends its own line, so two changes that both need room no longer conflict.
