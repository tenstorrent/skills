# Source ownership and improvements

`tenstorrent/skills` is the canonical source for this plugin's prompts, skill
instructions and investigation runner. Edit those files here and submit a PR.
Git history preserves the provenance of the earlier standalone prompt imports.
There is no external prompt-copying or synchronization step.

[skills-autoimprove](https://github.com/tenstorrent/skills-autoimprove) owns case
curation, prepared source snapshots, evaluators, results and scheduled improvement
flows. It fetches a skills revision once per run and records the exact commit.
Candidate advice is developed and committed in a skills branch, tested through
this plugin, and submitted with the observed case results. A single-case result
is evidence for that case, not a claim of broad benchmark improvement.

## Runner contract

`skills/autodebug/scripts/autodebug.sh` runs either bundled investigation:

```bash
/path/to/autodebug.sh --agent codex --model MODEL --effort high --events -- "symptom"
/path/to/autodebug.sh --task autotriage --agent claude --model MODEL --effort high --events -- "Read AUTOTRIAGE_INPUT.md"
```

Invoke it from the source tree to investigate. Reports are `AUTODEBUG.md` and
`AUTOTRIAGE.md`. AutoTriage's interactive skill remains usable in the calling
session; `--task autotriage` provides the fresh-process path for harnesses.

`--events` selects native Codex JSONL or Claude stream-json output. Callers retain
events and stderr, check terminal provider status and the generated report, and
record actual model/session metadata. A zero exit code alone is insufficient.

The process inherits caller configuration, including isolated `CODEX_HOME` or
`CLAUDE_CONFIG_DIR`. Repeat `--agent-arg VALUE` to pass literal CLI arguments such
as configuration files. These arguments are explicit caller overrides; do not
use them to silently change the requested model or weaken an evidence boundary.
Do not override the event format when the harness requires structured results.

Benchmark isolation and ground-truth exclusion belong to the calling harness.
The default workspace-write/inspection-only launch does not establish that
external answer files or network sources are unreadable. Preserve session
persistence when retained tool evidence is required.

## Validation and release

Preserve prompt placeholders and report contracts. Bump both host manifests and
add a changeset when installed content changes. Run the repository's plugin,
version and launcher checks. Submit changes through the normal reviewed PR path;
case registries, generated results, credentials and local configuration stay out
of this self-contained plugin.
