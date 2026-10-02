---
"tt-project": patch
---

`tt-project`: the daemon flags done code tasks whose branch holds work no remote has. When a code
task hands off done, and hourly for code tasks done in the last 14 days, it fetches (30 s timeout)
and checks each branch. Work already on a remote, including work a reviewer rebased, amended,
squashed or batched onto the delivery branch, is not flagged; nor is a task an unfinished task still
needs, a task a done review names, or work finished before the check first ran. Each new flag posts
one coordinator event; `ttp status` and the web app count it until the work reaches a remote, the
task is cancelled or it ages out. Nothing is pushed automatically.
