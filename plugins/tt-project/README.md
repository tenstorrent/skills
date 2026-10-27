# tt-project

Manages long-running, self-driving projects, sort of like Cursor and Claude projects, but they run locally, not in the cloud. 
Start a new project, give it a name and a description of what it should manage and how it should behave, and a custom harness will be generated to manage your project. Not only for coding! It can handle finance, legal, data center, any project. 

Example 1: 

``` 
new tt-project named ltx25 to manage all of my LTX 2.5 related work in the Metal repo. The project should run
on box-a, and can use box-a, box-b and exabox box-c for device work. Research and implement
optimizations. Integrate relevant optimizations from other DIT models, and explore novel optimization ideas.
Use profilers and profiling skills to pinpoint bottlenecks and address them. Babysit all of my LTX 2.5 related PRs.
Any PR you open must stay in draft form until I review and move it out of draft. Only I'm allowed to merge.
<these last two sentences are default, but doesn't hurt to be specific>. Watch git and Jira for LTX 2.5 related
issues and address them automatically.
```

Example 2: 

```
new tt-project named Tax to handle tax preparation. Running on this Mac.
Look in the Downloads folder for the 2025 and 2024 tax returns. All of the supporting docs are in the
Tax docs 2025 zip. Make sure there are no mistakes in the 2025 or 2024 return. Make a systematic plan and break
down the checks for several agents to do. Report to me if you find any mistakes. The task stops when you're
satisfied that everything's been thoroughly checked.
```

The project is run by a coordinator agent running as a background service and waking up periodically to check on the project. So it runs independently of your chat whenever the machine is on, but you interact with it through your chat. You give it instructions or ask it questions and it responds when it needs to update you in the chat. The background coordinator listens to your instructions from chat, plans the work, hands it to headless workers in isolated git worktrees, watches pull requests and Jira and output logs to find and auto-fix issues, whatever responsibilities you gave it. It never stops, it keeps finding valid work to do, and if it finds it, it does it, unless you give it a reachable goal state or ask it to stop.

Features:
- Supports: Claude Code, Cursor and Codex.
- Manage many projects at the same time from many chats.
- It's a meta-harness that generates a custom harness for each of your projects.
- Very flexible, so it can manage software, legal, or finance projects.
- Your brief and subsequent messages specify how it behaves; if you don't like how it behaves, you tell it to behave better.
- Each project has its own memory and remembers your preferences and lessons learned.
- The project self-improves periodically by assessing its own performance.
- Safe(ish?). It doesn't merge PRs or talk to humans in your name. It leaves sensitive operations to you, unless you tell it otherwise.
- Changes are reviewed by a review agent before they're pushed.
- Each project has a Web UI dashboard that you can check to see progress, critical messages, token budget, etc.
- It keeps going when you close your computer (if it's running on an always-on box), or even if you close the chat.
- Any later chat can reconnect to it by project name.
- The chat runs on your Mac (though it can run on a remote box as well), but the coordinator can run on a remote always-on box (or on your Mac as well).
- You can also tell it to move the coordinator from the box to your Mac or vice versa, or to any other box, if needed.
- Works best if you have Glean MCP enabled
- Leverages existing skills, understands TT dev workflows, machine setup, etc.
- Will stay within dollar budgets on usage billed plans and daily/weekly budgets on prepaid plans.
- Budgets are set per project and global totals of all projects.
- It throttles, stops and restarts work as token availability changes.
- Switching between plans is also fine. 
- If you want a project to go faster, just tell it to increase its budget.

## Install

Needs Python 3.9+, `git`, and Claude Code, Codex or Cursor. In a terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/tenstorrent/skills/smartonTT/tt-project/plugins/tt-project/install.sh | TT_PROJECT_REF=smartonTT/tt-project sh
```
Or just tell your agent "install tt-project from https://github.com/tenstorrent/skills".

Then reload your agent session. 

Rerun the above to update to the latest version of tt-project. 

If you're starting to work on a remote box you haven't worked on before (not project specific), you can say in the chat "/tt-project setup box box-a" and it will ask you a few questions and set up what you like: claude, claude plugin for cursor, tt-device-mcp, and other options. You can also tell it to set up things that are not part of the skill. 

## Learn more

- [Guide](docs/guide.md): how it works, budgets, model tiers, parallel work, notifications,
  security and limits.
- [Install](docs/install.md): what the installer does, manual steps, update, uninstall, known issues.
- [Providers](docs/providers.md): what each agent CLI supports.
- [Design notes](docs/design.md): for people changing tt-project itself.
