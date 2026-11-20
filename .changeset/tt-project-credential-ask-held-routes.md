---
"tt-project": patch
---

`tt-project`: the coordinator's ask gate refuses a credential or sign-in ask for a model or API service until it names the held routes it checked (configured providers and gateways, stored secrets, other held machines) and how each went. It also refuses one, naming the route, while the config holds a provider or gateway endpoint that serves the model the ask names, unless the ask reports that route's failure. Asks to rotate or revoke a leaked key, and asks for other credentials (an ssh key, a tool's login), are still sent.
