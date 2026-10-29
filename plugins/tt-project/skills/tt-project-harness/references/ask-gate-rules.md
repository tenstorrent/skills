# Project ask-gate rules

A project adds its own ask_user refusal rules in `project.json` under `ask_gate.refuse`, with no runtime edit: use it when the user says "don't ask me about X" for a whole class of asks, so the rule is enforced instead of only advised in a prompt.
Each entry is `{label, all_of, unless, pass_blocking, override_field, reason}`; only `all_of` and `reason` are required.
The coordinator's ask_user gate (runtime `coordinator.py`, `_project_refused_ask`) refuses an ask when every `all_of` regex matches its text, recommendation and least_disruptive together (case-insensitive), its `blocking` is not in `pass_blocking`, `unless` (a regex) does not match, and `override_field` (an ask field such as `not_own_call`), if set, holds fewer than 40 characters.
A refusal reads `ask_user rejected: <reason>` and counts under `label` in the daily review's refused-asks line, like the built-in checks.
A malformed entry (a bad regex, no `all_of` or `reason`, a wrong type) is skipped with a warning in `daemon.log` and in `ttp config` problems; it never fails a turn, and the other entries still apply.
With no entries the gate is unchanged.
The rule says what the coordinator should do instead in `reason`, since the refusal is all it sees.

A generic example: the coordinator decides routine benchmark spend itself, unless it is a first-time purchase, and an ask may still go out with a reason why the call is not the project's own.

```json
{"ask_gate": {"refuse": [
  {"label": "routine benchmark spend",
   "all_of": ["\\bbenchmark", "\\b(spend|budget|usd|cost)\\b"],
   "unless": "first[- ]time",
   "pass_blocking": ["review", "merge"],
   "override_field": "not_own_call",
   "reason": "routine benchmark spend is the project's own call: decide it, memory_add the decision and notify at severity low"}
]}}
```

No ask blocks. An ask the gate lets through stores its `recommendation` as its default; unanswered
after `coordinator.ask_timeout_h` (0.5 h; 0 off) the daemon resolves it to that (`askauto.py`). Asks only
the user can answer stay open: review, merge, another human's request, a recommendation that acts as the
user, a credential or money grant, an irreversible step that does not decline, and a restriction ask
whose recommendation would loosen it (only the user's own words loosen a restriction). A refusal rule therefore
matters most for asks the timer would leave open, and a good recommendation matters for every ask.

The user's own rules belong in their project's `project.json` and charter, never in the plugin's defaults.
