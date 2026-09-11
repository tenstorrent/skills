# Contributing to Tenstorrent skills

This repository contains a marketplace of independently installable plugins for
Tenstorrent development, debugging, model bring-up, and code review. Contributions
to skills, reference material, scripts, tests, and documentation are welcome.

## Getting started

1. Fork the repository and create a branch from `main`.
2. Find the plugin or skill you want to change in the [README](README.md).
3. Read [AGENTS.md](AGENTS.md) and any instructions in the directory you are changing.
4. Keep each pull request focused on one logical change and describe its effect on users.

For bugs and feature requests, open a GitHub issue. Include the affected plugin and
version, agent host, expected behavior, and a reproducible example when possible.
Report security vulnerabilities through the process in [SECURITY.md](SECURITY.md).

## Where to make changes

- The canonical review skills live in `skills/<bucket>/<skill-name>/`. Edit these
  files, then run `python3 scripts/sync_review_plugin.py` to regenerate the packaged
  copy. Do not edit `plugins/tt-review-skills/skills/` by hand.
- Other plugins keep their skills and supporting files under `plugins/<plugin-name>/`.
  Follow the plugin's `SYNC.md` and source manifest when updating imported material.
- Keep each plugin self-contained. Declare dependencies on other optional plugins
  and explain them in the user-facing installation instructions.
- New plugins need both host manifests, matching entries in both marketplace
  catalogues, an explicit rule in [.github/CODEOWNERS](.github/CODEOWNERS), and a
  README catalogue entry. Follow the full checklist in [AGENTS.md](AGENTS.md).

Preserve the marketplace's installation contract: users choose optional plugins;
the finder recommends them and obtains permission before installation.

## Versions and validation

When installed plugin content changes, increase its version in both
`.codex-plugin/plugin.json` and `.claude-plugin/plugin.json`. Add a changeset under
`.changeset/` describing the user-visible change. Repository documentation changes
that do not change installed plugin content do not need a plugin version bump.

Use Python 3.11, matching CI, and an activated virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install pytest==9.1.1 pyyaml==6.0.3
python scripts/sync_review_plugin.py --check
pytest tests/
```

After committing plugin changes, check version bumps against the current target
branch. If `upstream` points to `tenstorrent/skills`:

```bash
git fetch upstream main
python scripts/check_plugin_versions.py upstream/main
```

Use `origin/main` instead if `origin` points to `tenstorrent/skills`. When the Claude
Code CLI is available, also run `claude plugin validate . --strict`. CI additionally
checks workflow syntax, shell scripts, and relative Markdown links and anchors.

Describe the checks you ran and their results in the PR. For changes that affect
model execution, hardware behavior, or review quality, include the relevant manual
validation and its environment. Be explicit about checks you could not run, and
make performance claims only when supported by measurements.

## Licensing and attribution

By submitting a contribution, you agree to license your original contribution
under the [Apache License, Version 2.0](LICENSE), unless explicitly stated otherwise.
See [LICENSE_understanding.txt](LICENSE_understanding.txt) for the accompanying
Tenstorrent rights clarification.

When importing or adapting material, preserve its copyright and license notices.
Record its upstream source, revision, and contributors in [SOURCES.md](SOURCES.md)
or the relevant plugin's source manifest. Update [NOTICE](NOTICE) and the license
texts in `LICENSES/` when applicable. Follow the directory's vendoring review rules
before adding material from another repository.

## Community and review

Participation is covered by the [Code of Conduct](CODE_OF_CONDUCT.md).
The maintainers for each area are listed in [CODEOWNERS](.github/CODEOWNERS).
Shared marketplace and CI changes should involve both catalogue owners.
