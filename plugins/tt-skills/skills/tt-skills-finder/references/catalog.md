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

There are no model-bringup or autodebug plugins in the published catalogue yet. Do not invent them;
mention that they are planned only when it directly answers the user's question.
