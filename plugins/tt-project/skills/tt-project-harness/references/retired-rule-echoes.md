# Retired-rule echoes

A rule the user retired keeps binding while a copy of it survives somewhere else. So whenever text is retired, the daemon records it:

- a coordinator `charter_update` with `quote` (the quoted span) or `replaces` (the replaced section's body);
- a `memory_add` with `supersedes`, or `ttp memory <name> "<merged>" --supersedes <entry>...` (each replaced entry's text);
- a user Correction fact (`Correction (user msg #N): <right>, not <wrong>`) whose wrong part is prose of at least 3 words. A bare value ("not low") is left to the spec and ask check that catches restated corrected values.

From the retired text it keeps up to 8 distinctive phrases: quoted and backticked spans, config keys and identifiers (a dot or underscore inside), and the first sentence (its first 12 words). A phrase the replacing text still holds is dropped, since both rules say it. So is plugin vocabulary: a lone identifier or `ttp <command>` name found in the tt-project runtime source, the plugin template or an unedited template file (`start_when`, `memory_add`, `pr_branch`, `ttp list`), since alone it matches unrelated text. The plugin's prose is no vocabulary, and a Correction's wrong part (such as `sudo -n true`) never is. A retirement left with no phrase is not searched. Records are kept 30 days (`retired_rules` in the project's kv store).

Each coordinator turn it searches, model-free and case-insensitively (whitespace collapsed):

- live memory entries (the store that both the workers' and the coordinator's copies come from);
- charter sections;
- schedules: every string in the payload (command, precheck, heal check, prompt) and the description;
- open tasks: spec and `start_when` probe;
- config values;
- the project's own files, up to 400 (256 KB each, binary files skipped): everything under `harness/` except what is searched in its own form or is an archive or the installed runtime (`memory/`, `runtime/`, the charter and its history, `MEMORY.md`, `project.json`, `schedules.json`, `settled_areas.json`), so harness scripts, code guards, tools, hooks and prompts; below the project root `.claude/skills`, `.claude/hooks`, `.claude/commands`, `.claude/agents`, `.agents/skills`, the CLAUDE.md and AGENTS.md files; and each file or folder listed in `coordinator.echo_paths` (relative to the project root, never outside it), for recovery code or skill text kept elsewhere in the project's repo. A harness file the project never edited (equal to the harness's `upstream` template snapshot or the plugin template) is skipped: only a project-tuned copy can be fixed. A file hit names its path and line.

It does not search the replacing text (its new entry, or its words wherever they stand) or the archives (`memory/archive`, the charter history). For a Correction, a match right after a negation ("never <phrase>") agrees with it and is no hit.

Hits go into the digest's `Retired-rule echoes` section: the place, the matching line and which retirement it echoes, at most 10 lines including the header, then a count of the rest. A hit still there one turn after it was first shown is marked `[still here, N turns]`. When a memory entry hits, the last line names every such entry (shown or not) for `memory_forget`, or a `memory_add supersedes` that keeps what is still right, so copies do not pile up. An empty scan gives no section. The section carries its own instruction: fix each hit in that turn with `memory_forget` or `memory_add supersedes`, `charter_update quote`, `schedule_set`, `task_update` or `config_set`.

It is a notice only: it posts no event, wakes nothing, holds nothing and adds no model call.

Known gap: a task whose spec quotes the old rule in order to remove it is listed too. Leave it, or finish the task.
