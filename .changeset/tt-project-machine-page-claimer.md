---
"tt-project": patch
---

`tt-project`: shared-machine pages reach the right chat. A machine-change, machine-condition or outage-task page is claimed only by its target project's daemon (a target on another host is relayed, as before). A user page goes to the chat of the project that made the entry, else the machine's recovery owner, else its fallback, whichever is first on this host; with none of them here, only a project whose charter lists the machine may deliver it. Otherwise it is kept unsent, logged once and listed in `ttp status`, never posted in an unrelated co-tenant project's chat.
