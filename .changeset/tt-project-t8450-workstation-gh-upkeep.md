---
"tt-project": patch
---

tt-project: the workstation gh queue also runs draft-PR upkeep: `gh pr edit` of the title or body only, a reply to a PR review comment (`gh api -X POST .../pulls/<n>/comments/<id>/replies -f body=...`), and `gh pr close` of a draft the workstation's own gh user opened, checked by a read just before the close. Bodies, replies and close comments end with the AI-authorship tag; reviewers, labels, base, `--delete-branch`, `pr ready` and merges stay refused.
