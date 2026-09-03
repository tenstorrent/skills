# Tenstorrent plugin catalogue

The marketplace is registered from `tenstorrent/skills`. Adding it makes the catalogue discoverable;
it does not authorize installation or use of every optional plugin.

## `tt-review-skills`

Use for reviewing Tenstorrent pull requests or diffs, especially tt-metal, TTNN, Metalium, LLK,
multi-chip, model, serving, trace, precision, performance-claim, test-coverage, and L1-memory changes.

The plugin contains a shared review contract and router plus focused domain review skills. It emits
evidence-backed findings; it does not implement or silently post changes. It can also help analyze
CODEOWNERS and propose a lower-reviewer PR split.

Do not route ordinary implementation, debugging, model bring-up, or optimization work here merely
because the code could later be reviewed. Recommend this plugin when review is the user's task.

Install or enable it only after the user chooses it:

- **Codex / ChatGPT desktop:** select `tt-review-skills` under **Tenstorrent Skills** in the Plugins
  Directory.
- **Claude Code:** run `/plugin install tt-review-skills@tenstorrent-skills` after adding the
  `tenstorrent/skills` marketplace.

## `tt-autodebug`

Use for tt-metal or TTNN code issues and hangs that should first be debugged by inspection without
running the target code, then fixed through experiments that distinguish the root cause from
plausible alternatives. The plugin contains:

- `autodebug`, which diagnoses code issues in a fresh inspection-only Codex or Claude session so
  the investigation does not consume the calling agent's context;
- `autotriage`, which diagnoses hangs from tt-triage evidence and concrete source contracts; and
- `autofix`, which tenaciously proves or disproves hypotheses with experiments until it finds and
  addresses the root cause.

Do not route routine coding or code review here. Recommend this plugin when the problem is genuinely
unclear, evidence-heavy, hardware-adjacent, or likely to need repeated investigation and repair.

Install or enable it only after the user chooses it:

- **Codex / ChatGPT desktop:** select `tt-autodebug` under **Tenstorrent Skills** in the Plugins
  Directory.
- **Claude Code:** run `/plugin install tt-autodebug@tenstorrent-skills` after adding the
  `tenstorrent/skills` marketplace.

Once the user installs the plugin, its three skills support normal automatic selection. AutoDebug
does not require confirmation for each run; its child session remains inspection-only.

There is no model-bringup plugin in the published catalogue yet. Do not invent it; mention that it
is planned only when it directly answers the user's question.
