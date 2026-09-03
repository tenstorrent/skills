---
name: tt-skills-finder
description: Find and recommend optional Tenstorrent plugins for tt-metal work. Use when a task could benefit from specialized review, debugging, model-bringup, optimization, or hardware guidance, or when the user asks what Tenstorrent skills are available. Do not install or invoke a recommendation without the user's action or explicit permission.
---

# Find Tenstorrent skills

Recommend the smallest focused plugin that materially improves the user's tt-metal task. This skill
is a catalogue guide, not an aggregate workflow.

## Find a match

1. Identify the user's concrete task and whether a specialized workflow would add real value.
2. Read `references/catalog.md` for the current plugin catalogue and routing cues.
3. Check the host's available or installed skills when that information is exposed. Do not claim a
   plugin is installed when you cannot verify it.
4. Recommend at most three plugins, ordered by relevance. For each, give its name, one sentence on
   why it fits, and the user action needed to install or enable it.
5. If nothing is a strong match, continue with normal capabilities without forcing a recommendation.

## Permission boundary

- A recommendation is not permission to install, enable, or invoke a plugin.
- Do not run an installation command, change plugin configuration, or begin an optional plugin's
  workflow based only on your own recommendation.
- If the user explicitly asks to install or enable a plugin, follow the host's normal installation
  flow and surface any trust or authentication prompt.
- If a matching plugin is already installed and the user's request clearly invokes its capability,
  normal skill routing may use it. Preserve any separate authorization gate inside that workflow.

Keep the recommendation user-facing. Do not expose marketplace internals unless they help the user
complete the installation.
