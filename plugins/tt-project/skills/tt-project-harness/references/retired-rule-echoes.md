# Retired-rule echoes

A rule the user retired keeps binding while a copy of it survives somewhere else. So whenever text is retired, the daemon records it:

- a coordinator `charter_update` with `quote` (the quoted span) or `replaces` (the replaced section's body);
- a `memory_add` with `supersedes`, or `ttp memory <name> "<merged>" --supersedes <entry>...` (each replaced entry's text);
- a user Correction fact (`Correction (user msg #N): <right>, not <wrong>`) whose wrong part is prose of at least 3 words. A bare value ("not low") is left to the spec and ask check that catches restated corrected values.

From the retired text it keeps up to 8 distinctive phrases: quoted and backticked spans, config keys and identifiers (a dot or underscore inside), and the first sentence (its first 12 words). A phrase the replacing text still holds is dropped, since both rules say it. Records are kept 30 days (`retired_rules` in the project's kv store).

Each coordinator turn it searches, model-free and case-insensitively (whitespace collapsed):

- live memory entries (the store that both the workers' and the coordinator's copies come from);
- charter sections;
- schedules: every string in the payload (command, precheck, heal check, prompt) and the description;
- open tasks: spec and `start_when` probe;
- config values.

It does not search the replacing text (its new entry, or its words wherever they stand) or the archives (`memory/archive`, the charter history). For a Correction, a match right after a negation ("never <phrase>") agrees with it and is no hit.

Hits go into the digest's `Retired-rule echoes` section: the place, the matching line and which retirement it echoes, at most 10 lines including the header, then a count of the rest. A hit still there one turn after it was first shown is marked `[still here, N turns]`. An empty scan gives no section. The section carries its own instruction: fix each hit in that turn with `memory_forget` or `memory_add supersedes`, `charter_update quote`, `schedule_set`, `task_update` or `config_set`.

It is a notice only: it posts no event, wakes nothing, holds nothing and adds no model call.

Known gap: a task whose spec quotes the old rule in order to remove it is listed too. Leave it, or finish the task.
