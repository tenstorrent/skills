# Maintaining this repository

This repository is a marketplace of independent Tenstorrent plugins. Keep the user contract simple:
one repository to register, a small finder installed by default where the host supports that, and
all substantive workflows installed only when the user chooses them.

## User and permission contract

- `tt-skills` contains discovery guidance only. It may recommend an optional plugin, but must not
  install, enable, or invoke one without the user's action or explicit permission.
- Registering the marketplace is not permission to use every plugin in it.
- Installing an optional plugin permits its skills to participate in normal automatic selection.
  The consent boundary is installation, not each invocation.
- Optional plugins must stay independently installable. Do not create an aggregate plugin that
  silently loads the full catalogue.

## Plugin boundaries

- Put each plugin under `plugins/<plugin-name>/` with both
  `.codex-plugin/plugin.json` and `.claude-plugin/plugin.json`.
- A packaged plugin must be self-contained: no paths outside its directory and no runtime reliance
  on another optional plugin unless that dependency is declared and visible to the user.
- Keep the Codex catalogue in `.agents/plugins/marketplace.json` and the Claude catalogue in
  `.claude-plugin/marketplace.json`. Their plugin names and source paths must agree.
- Every plugin needs an explicit CODEOWNER. Shared marketplace and CI changes need both catalogue
  owners.
- Scope implementation rules to the plugin they protect. In particular, the constraints under
  `skills/` apply to `tt-review-skills`, not to future model-bringup or debugging plugins.

## Canonical and packaged files

The bucketed `skills/` tree is the canonical source for `tt-review-skills` and remains directly
pin-able by gh-aw. `scripts/sync_review_plugin.py` creates the flat, self-contained plugin copy.
Never edit `plugins/tt-review-skills/skills/` by hand.

Other plugins keep their canonical skills inside their own plugin directory unless they have a
documented generator. Prefer one canonical implementation plus an enforced generated copy over
two hand-maintained versions.

## Adding or changing a plugin

1. Add or update both plugin manifests and both marketplace entries.
2. When installed plugin content changes, bump its version in both manifests. Claude caches
   explicit plugin versions, and CI rejects changed content with an unchanged version.
3. Add the plugin's CODEOWNERS rule before asking for review.
4. Update the root README's user-facing catalogue and installation instructions.
5. If changing a canonical review skill, run `python3 scripts/sync_review_plugin.py`.
6. Run the package-sync and version checks and `pytest tests/`; also run host plugin validators
   when available.

Do not claim tests, model quality, hardware behavior, or performance that was not measured.
