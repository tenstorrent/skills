---
"tt-project": patch
---

`tt-project`: new `ttp bridge`, an ssh access bridge: lab boxes log in to other hosts through a link to the workstation's ssh agents, with no key stored on the box, failing closed when the link is down; it forwards the service's own agent (`"env"`) and relays hosts the workstation reaches through one interface. Optional offline mode (off unless the bridge config has a `"cert"` section): each box keeps its own key and a short-lived ssh certificate (72 h by default, listed principals, the box's source address, no forwarding) that the workstation renews at half life from a user service; signers are a CA in the workstation's agent (`ssh-keygen -s -U`, no private key file) or an outside command for a central CA. Over-broad or foreign certificates are refused before install; while the link is down the box's ssh block uses the certificate and fails closed once it ends. `ttp bridge cert status|renew|remove|trust --print|schedule`; nothing edits `authorized_keys`.
