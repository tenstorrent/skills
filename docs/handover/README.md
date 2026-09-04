# tt-debug-tools — handover to a machine with a device

Goal: a `tenstorrent/skills` plugin holding one skill per Tenstorrent debug
tool. Each skill teaches an agent to drive its tool and read the output.
Nothing higher-level — no workflows, no loops, no router.

Two skills of seventeen are written. The eval harness works. **No fixture has
been captured, because the machine this was built on has no device.** That is
the work waiting on the other side.

## 0. First action — untrack these three documents

`docs/handover/` is a **transport directory**, not repo content. The three files
in it were committed only so this work could move to a machine with a device.
Once you have the clone, take them out of git tracking and keep them on disk:

```bash
git rm -r --cached docs/handover
printf 'docs/handover/\n' >> .git/info/exclude
```

`.git/info/exclude` is per-clone and never committed, so the directory stops
appearing in `git status` without adding a `.gitignore` entry that would outlive
the handover. Do this before your first commit, or these documents end up in a
PR that has nothing to do with them.

The three files:

| File | What it is |
|---|---|
| `README.md` | this document |
| `debug-tool-support.md` | every tool: env vars, commands, output shapes, traps, fixtures, coverage elsewhere. The verified tool facts. |
| `debug-skills-plan.md` | the 17-skill set, the merge rule, the budgets, the landing order, the MISSING register. |

Everything else under `plugins/tt-debug-tools/` **is** repo content and stays
tracked.

### Where this came from

`debug-tool-support.md` and `debug-skills-plan.md` were written in
`tenstorrent/tt-buddy` at `ba90214` and copied here for transport. They are not
tracked in tt-buddy either. If they need a permanent home later, that decision
is open — they describe work in this repo, so `docs/` here is defensible, but
they are planning records rather than catalogue content.

## 1. Machine prerequisites

```bash
tt-smi -s                      # a healthy board; note board_type
echo $TT_METAL_HOME            # a built tt-metal checkout
which claude codex             # both CLIs, both authenticated
python3 --version              # 3.10+
```

tt-metal must be built **with the unit tests and the programming examples** —
the fixtures come from upstream gtest cases. Confirm the right build flags
against the current `build_metal.sh`; these binaries have to exist:

```
$TT_METAL_HOME/build/test/tt_metal/unit_tests_debug_tools
$TT_METAL_HOME/build/test/tt_metal/unit_tests_noc_debugging
$TT_METAL_HOME/build/test/tt_metal/unit_tests_inspector
$TT_METAL_HOME/build/programming_examples/matmul_multi_core
$TT_METAL_HOME/build/programming_examples/profiler/test_full_buffer
```

Also `python -m pip install -r $TT_METAL_HOME/tools/triage/requirements.txt`.

## 2. What exists

```
plugins/tt-debug-tools/
  .claude-plugin/plugin.json      14 lines   passes claude plugin validate --strict
  .codex-plugin/plugin.json       33 lines   schema copied from PR #3's real file
  skills/tt-triage/
    SKILL.md                     127 lines   router; cap is 130
    references/flags.md           2054 B
    references/read-order.md      2716 B
    references/scripts.md         3665 B
    references/signals.md         4374 B     cap is 4500 — little headroom left
    manifest.yaml                            declared env/flags/fixtures
  skills/tt-noc-dump/
    SKILL.md                      86 lines
    manifest.yaml
  tests/
    conftest.py                  306 lines   agent fixture, schema, fixture loader
    capture_fixtures.sh          112 lines   the device step — not yet run
    fixtures/README.md                       fixture contract + program per scenario
    test_tt_triage_usage.py       6 tests    no device
    test_tt_noc_dump_usage.py     4 tests    no device
    test_tt_triage_reading.py     2 tests    needs fixtures; skips today
```

### Two kinds of test, and what each proves

**Usage tests** ask an agent how to drive the tool. They catch invented
environment variables, wrong gtest filters, and answers that contradict a
documented constraint. They found one real skill defect: `--remote-exalens` was
named in `SKILL.md` without its `tt-exalens --server` prerequisite, which lived
only in `references/flags.md`. They do **not** prove the tool works.

Last full run: 10 passed, $1.49, 351s on Opus, sequential.

**Reading tests** hand the agent output captured from a real run and ask what
happened. This is the test that matters. It needs no device at test time — the
output is a committed file — but the file must be captured on hardware.

Both carry `pytest.mark.agent`, so `-m "not agent"` deselects everything and the
per-PR suite stays free and offline.

## 3. First actions on the device machine

```bash
# 1. wire Claude (session-scoped, no install)
claude plugin validate plugins/tt-debug-tools --strict

# 2. confirm the usage suites still pass on this machine
python3 -m pytest plugins/tt-debug-tools/tests -q -p no:randomly \
  -k usage --agent-model <cheap-model>

# 3. capture fixtures — the actual point of moving
TT_METAL_HOME=$TT_METAL_HOME plugins/tt-debug-tools/tests/capture_fixtures.sh

# 4. author expected.json per scenario, by hand, from each output.txt

# 5. the reading tests should now run instead of skipping
python3 -m pytest plugins/tt-debug-tools/tests -q -k reading
```

### The capture step

`capture_fixtures.sh` runs nine scenarios across seven skills. Each writes
`fixtures/<skill>/<scenario>/` with `cmd.sh`, `output.txt` and `meta.json`.
Provoking programs and scenarios are tabulated in `tests/fixtures/README.md`.

**Known defect in that script:** the `tt-triage/halted-core` scenario
backgrounds the sanitize gtest, `sleep 20`, then runs triage. Twenty seconds is
a guess made without hardware. Replace it with a poll — for the watcher log
appearing, or for the dispatch timeout — before trusting the capture. If triage
runs too early it reports a healthy device; too late and the process is gone and
triage reports nothing. Both failures look like a bad fixture rather than a bad
sleep.

**Authoring `expected.json` is the ground truth.** Do not generate it with the
same agent the test grades — the test would measure self-agreement instead of
correctness. Read `output.txt` and write down what it actually shows. Only pin
the keys the scenario knows:

```json
{
  "fault_found": "yes",
  "primary_signal": "dump_callstacks",
  "location_contains": ["NCRISC"],
  "evidence_strength": "evidence"
}
```

`fixtures/tt-triage/healthy-run` is the **negative control** and is not
optional: a skill that reports a fault for every input passes every positive
fixture.

## 4. Agent wiring

### Claude

Session-scoped, no install, always reads the working tree:

```bash
claude -p "<scenario>" --plugin-dir plugins/tt-debug-tools \
  --json-schema "$(python3 -c 'import sys,json;sys.path.insert(0,"plugins/tt-debug-tools/tests");from conftest import ANSWER_SCHEMA;print(json.dumps(ANSWER_SCHEMA))')" \
  --output-format stream-json --verbose --restricted --permission-mode dontAsk
```

`--restricted` removes Bash, so the agent reports the command it would run
rather than running it. That is what makes the usage tests device-free.

### Codex

Installed through a marketplace, already wired on the old machine and needing
re-creation here:

```bash
MP=~/.codex/dev-marketplaces/tt-debug-tools
mkdir -p "$MP/.agents/plugins" "$MP/plugins"
ln -sfn "$PWD/plugins/tt-debug-tools" "$MP/plugins/tt-debug-tools"
# marketplace.json: name tt-debug-tools-dev, plugins[0].source =
#   {"source":"local","path":"./plugins/tt-debug-tools"}   <-- relative, see gotchas
codex plugin marketplace add "$MP"
codex plugin add tt-debug-tools@tt-debug-tools-dev
```

Run a scenario:

```bash
codex exec --json --skip-git-repo-check --output-schema schema.json \
  -s read-only -o last.json "<scenario>" </dev/null
```

Confirmed working: the plugin installs, all skills and references arrive, the
schema validates, and the answer was correct on the one scenario tried.

## 5. Gotchas, all found the hard way

**Claude `skills[]` must be `./`-prefixed paths.** Bare names produce a silent
load failure — `plugins: []` in the init event, no error, and the agent answers
plausibly from its own knowledge. `claude plugin validate --strict` catches it.

**Codex uses a different manifest schema.** `"skills": "./skills/"` is a single
directory string, not an array, plus a required `interface` block. Mirroring the
Claude schema does not work.

**Codex marketplace `source.path` must be relative.** An absolute path is
accepted by `marketplace add` and the plugin then simply does not appear in
`codex plugin list`. No error.

**Codex caches the plugin. Repo edits do not reach it.** `codex plugin add`
copies into `~/.codex/plugins/cache/<marketplace>/<plugin>/<version>/`. After
every skill edit:

```bash
codex plugin remove tt-debug-tools --marketplace tt-debug-tools-dev
codex plugin add tt-debug-tools@tt-debug-tools-dev
```

The `--marketplace` flag is required on remove. `marketplace upgrade` does not
help — that is for Git sources. This is the worst trap of the set: you edit a
skill, re-run, and silently test the old version.

**The answer schema must avoid free-form maps.** OpenAI structured outputs
reject an object with arbitrary keys, so `env` is an array of `{name, value}`.
A schema that validates on Claude can 400 on Codex.

**The agent's working directory must be neutral.** Run it from the repo and it
Reads `SKILL.md` directly, answers correctly, and never dispatches — so the
dispatch assertion measures nothing. `conftest.py` defaults to an empty temp
dir; `--agent-cwd` overrides.

**Codex has no Skill tool call.** It shells out to `sed -n '1,240p' .../SKILL.md`.
The Claude fixture asserts on a `Skill` `tool_use` in the transcript; a Codex
fixture needs to detect the file read instead.

**Claude addresses plugin skills as `plugin:name`.** Compare on the bare name.

**`codex exec` needs `--skip-git-repo-check`** outside a trusted directory, and
`</dev/null` or it blocks reading stdin.

**Assert on structure, never on phrasing.** "does not prove" and "rather than
proving" are the same claim; a keyword list catches one. Judgements are carried
by enums — `verdict`, `fault_found`, `evidence_strength`. One test was flaky for
exactly this reason and passed one run, failed the next, with no code change.

## 6. Budgets and repo rules

Enforced by the target repo's tests, not by convention:

| Unit | Limit |
|---|---|
| `SKILL.md` | ≤130 lines, tested. Target 120. |
| `references/*.md` | <4500 bytes each, tested |
| `name` | equals the directory name, globally unique across the repo |
| `metadata.tier` | one of `model \| op \| kernel \| process` |
| `metadata.upstream` | `{repo, ref, path}` list; `ref` a real 40-char SHA |

`metadata.upstream` is parsed by the drift audit — a malformed entry silently
drops that upstream. Every skill here is vendored; nothing is original work. A
fabricated SHA was caught once already.

Landing a skill also means crediting the source in `README.md` and regenerating
`SOURCES.md` with the drift audit's `--sources` mode.

## 7. What is left

Fifteen skills. Order and rationale in `debug-skills-plan.md`.
Next is `tt-watcher`, which is the budget stress test: five references under
4500 bytes each routed from a body under 120 lines. If it will not fit, folding
`watcher_dump` into it was wrong and it splits back out.

Open items nobody has closed:

| Item | Note |
|---|---|
| PR #3 in `tenstorrent/skills` | Adds `plugins/`, `scripts/`, both marketplace catalogues, CODEOWNERS, `validate.yml`. Open. Root catalogues and CODEOWNERS deliberately untouched here to avoid conflicting with it. |
| `metadata.tier` | Its four values are review-oriented. None describes a device tool. `process` is the least-bad fit. |
| Suite cost | ~$0.10–0.15 per test on Opus, sequential. 17 skills × ~5 tests ≈ $12 and ~50 min. `--agent-model` for cost; `pytest-xdist` for wall time, but that is a new non-stdlib dependency. |
| Codex `must_disable` | Came back as prose (`"watcher"`, `"profiler"`) rather than variable names, so `test_missing_barrier_suspected` would fail under Codex. Tighten the schema description or loosen the assertion. |
| `tests/` ships to users | Codex packages the whole plugin directory, so the installed cache contains `tests/`. Harmless, but it is dead weight in a user install. |
| No fixture for 8 tools | `tt-llk-sanitizer`, `tt-exalens`, `tt-dispatch-telemetry`, `tt-ttnn-debug-modes`, `tt-npe`, `tt-ttsim`, `tt-perf-counters`, `tt-operation-timeout`. Upstream ships no provoking program; one has to be authored. |
