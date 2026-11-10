---
"tt-project": patch
---

tt-project: the chat-side skill tells hosts with no background wake-up (Codex and others) to wait
for replies with one long blocking `ttp listen --once --timeout 600` in the foreground, its tool
call's own time limit and wait above the timeout, and never to start a command in the background
and check it every few seconds (each empty check re-reads the whole chat). A chat stays thin, and
one whose context grows large writes a short hand-over and continues in a fresh thread with the
same chat id and last id shown.
