---
"tt-project": patch
---

`tt-project`: a `ttp bridge` session whose reverse forward fails now ends the stale sshd session that a sleep left half-dead on the box. The cleanup login finds it by the `agent.<pid>` its agent link still points to (when that agent no longer answers) and by the session's own listen ports, ends only the account's own sshd sessions (never its own session or an ancestor), and kills one only while the ports stay held. It succeeds only once the ports are free, logs each held port with its pid, and cleans again after 5, 20 and 60 s before the supervisor takes over, instead of looping on the forward failure.
