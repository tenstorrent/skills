---
"tt-project": patch
---

`tt-project`: what a run posts where people other than the user read it ends with an AI-authorship tag, `delivery.ai_tag` (default `[authored by AI]`, `""` turns it off). Worker and coordinator prompts state the rule. The worker `gh` wrapper refuses `gh issue create`, `gh issue comment`, `gh pr comment`, `gh pr review` and comment, review or issue posts through `gh api` (REST and GraphQL) whose body does not end with it; a Claude Code PreToolUse hook does the same for connector tools that send or create a message, comment, issue, page or email (Slack, Jira, Confluence, mail). PR bodies, issue and PR body edits, commit messages and messages to the user are left as they are.
