---
"tt-project": patch
---

`tt-project`: device work on the GLM route (`route.glm_work.device`, rollout flag glm_device, off by default): Opus writes the plan, and GLM runs only that plan's steps. Reads run freely. A reset, power cycle, flash or firmware step needs an Opus step check, and power goes only through the project's approved routes. Each run gets an Opus audit afterwards.

The plan route keeps the person-contact guard: mail, raw sockets, databases and routed notes stay refused even if a plan step names them. ssh reaches only hosts the plan names, with no jumps or forwards. One strict ssh parser, shared by the hook, the runner and the plan check, fails closed: only a few harmless options, no option or `--` after the destination, and no local-command option (ProxyCommand and kin) anywhere. The route reuses the hook's shell parser and the shared read classifier. A plan step matches only its exact normalized command: no `git -C` rewrite, no trailing-note stripping, no shadow-path rewrite. Script and route names match strictly. A later verdict clears a missing audit. The plan check refuses an ssh step it cannot parse (for example one with a backquote) instead of skipping it.

Known gaps: a remote command that only mentions an option name is no longer refused by word; the strict parser is the single check. Style findings from earlier rounds on the plan module are left as documented gaps.
