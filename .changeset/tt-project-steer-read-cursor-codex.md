---
"tt-project": patch
---

`tt-project`: a Cursor or Codex worker run whose harness hook ran shows a mid-run update as read or unread, and counts toward the missed-update check, exactly as a Claude Code run does; the decision goes by the run's `steer.hooked` marker. A run whose hook never ran keeps the fallback (read once `ttp note` or `ttp steer` handed the update over, else unknown). Tests cover both providers with and without the marker; the provider docs' Cursor column says so.
