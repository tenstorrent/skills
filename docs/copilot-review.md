# Copilot review and skill smoke tests

Copilot code review uses `.github/copilot-instructions.md` and the repository's
`.github/skills/code-review/SKILL.md` to focus on marketplace contracts, executable
behavior, and evaluation evidence. This skill is repository review configuration;
it is not an optional marketplace plugin and does not change plugin versions.

## Enable reviews

A repository administrator can enable automatic Copilot reviews and **Review new
pushes** in GitHub's review rules. Use Balanced effort for substantial changes if
available under the organization's policy. Keep required deterministic CI and a
human code-owner approval. Adding these files does not change repository settings.

Copilot reads review instructions from the PR head. Review changes to these files
as policy changes. Inspect review-comment attributions and session logs to see
which skill and context Copilot actually used. Custom MCP servers are not required.

## Run a candidate smoke test

Install Python 3.11+ and an authenticated Copilot CLI. The runner targets CLI
**1.0.85** (`npm install --prefix /tmp/tt-copilot-cli @github/copilot@1.0.85`).
Use a token supported by Copilot CLI in `COPILOT_GITHUB_TOKEN`; the runner uses a
fresh `COPILOT_HOME`, so it does not reuse your interactive login/configuration.
A normal Actions `GITHUB_TOKEN` is not a substitute for Copilot authentication.

Validate cases without credentials or model calls:

```bash
python scripts/copilot_smoke.py --validate-only
```

Explicitly run paid model evaluations with a model ID available to your account:

```bash
python scripts/copilot_smoke.py \
  --copilot /tmp/tt-copilot-cli/node_modules/.bin/copilot \
  --model YOUR_MODEL_ID \
  --output /tmp/copilot-smoke-results.json
```

Use `--candidate /path/to/pr-checkout` to test another revision while keeping the
runner and expected answers in a trusted checkout. `--case finder-install-consent`
runs one case. `--timeout` bounds each model process; `--credits` sets a soft
per-case credit limit (not a hard billing cap). Each case starts a fresh session.

The runner stages only the selected skill Markdown under a temporary
`.github/skills` directory. It verifies that `copilot skill list --json` discovers
the enabled candidate paths, then supplies those exact instructions and references
in a tool-free prompt. Expected fields stay in the parent grader, outside the
agent's workspace and prompt. Candidate scripts, hooks, and plugin manifests are
not executed or installed. Shell, file, MCP, and delegation tools are unavailable
to the model. Run on a disposable machine when evaluating untrusted contributions;
a temporary directory alone is not an OS sandbox.

Results record the candidate Git commit, hashes of the actual Markdown and case
suite, CLI version, requested model, discovered skills, answers, and case status.
Modified working-tree content is represented by its hashes; the commit alone is
not a claim that the checkout was clean. Missing CLI, failed discovery, malformed
answers, timeouts, and an empty case selection fail instead of silently skipping.

## What this evidence means

- Discovery verifies that Copilot recognizes the staged skill files.
- Instruction replay checks guided answers against explicit expected fields.
- It does **not** establish automatic routing, improvement over an unskilled
  baseline, script execution, Claude/Codex plugin installation, or hardware behavior.
- Review explanations in the report as well as exact-field grades. A few passing
  answers are smoke evidence, not a comprehensive quality score.

Keep expected answers independently reviewed. Add cases for negative behavior and
ambiguous routing as well as successful examples. For before/after comparisons,
use the same trusted case suite, CLI version, and model on each candidate.

The normal `validate` job runs the harness's mocked unit tests and validates case
inputs without model calls. Paid evaluations remain explicitly invoked; no new
secrets, paid workflow, hardware access, or automatic installation is enabled by
this PR. Native host and device evaluations need separate environments.

## References

- [Customize Copilot code review](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/request-a-code-review/use-code-review)
- [Copilot agent skills](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-cloud-agent/add-skills)
- [Copilot CLI reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)
