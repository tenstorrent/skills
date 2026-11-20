# Live impact: interim mitigations

A hand-off states live impact only in the structured field `live_impact`: `"<resource> <what is degraded>"` or `{"resource": ..., "what": ...}`.
It goes at the top level of result.json (a list is allowed there), or on the follow-up that proposes the fix.
Free text is never parsed for it.
The daemon (runtime `mitigate.py`) then queues, model-free, a priority-1 mitigation task beside the fix, due `coordinator.mitigation_due_h` hours later (default 4; 0 turns it off).
The fix is the reporting code task itself (top-level field), the follow-up carrying the field, or none yet.
One open mitigation per resource; a mitigation's own hand-off files none.
Its spec asks for the least-disruptive safe workaround the charter or memory already approves (a restart, a manual escalation, rerouting work, a heal check), never a code fix.
Each live change on a shared machine goes in the change ledger (`ttp machines change add ... --undo ...`, with `--test` for a change made for a test), and every temporary measure in result.json `temporary_measures`: `[{"what": ..., "undo": ...}]`.
The blocker sweep and `ttp status` list each open mitigation with its due time.
Past due and not done, it raises one high `mitigation_overdue` event, which wakes the coordinator (a tricky turn the turn gates never skip; its effort is not raised).
Once it and its fix are done, one `mitigation_undo` event lists its measures and open ledger entries to undo after the fix is deployed.
