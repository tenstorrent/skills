---
"tt-project": patch
---

`tt-project`: Prompts and skills no longer ask the user to do, or offer to do, what tt-project can do
itself: the coordinator, workers and chat skill do it and say so, and ask only for real decisions. A
local forward to view a remote project's web app now opens without asking and stays up with
`ttp web <name> --tunnel --keep`. The kept tunnel also recognises a `com.tt-project.tunnel.<name>`
service set up by hand (a `localhost` forward, or one wrapped in a shell), adopting it when it already
forwards to the project and otherwise replacing it on its own local port. Tunnels that expose the
user's machine to others (reverse forwards) still need the user's OK. A test flags the main forbidden
phrasings.
