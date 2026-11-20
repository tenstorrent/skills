---
"tt-project": patch
---

`tt-project`: the retired-rule echo check no longer orders a fix when a task spec shares with a withdrawn rule only a script path run as a command (`bash tests/x.sh --quick`, `pytest tests/x.py`): an interpreter or test runner and option flags beside a path or file name add no rule wording, so such a match is advisory like a bare path. A match on the rule's own wording, with or without the path, is still a fix order.
