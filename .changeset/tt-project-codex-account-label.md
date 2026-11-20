---
"tt-project": patch
---

`tt-project`: the Codex provider reads its login from `$CODEX_HOME/auth.json` (default `~/.codex`) and labels the account `codex (<mode>) <hash>`, where the hash is a short opaque digest of the account id (or the API key), so two ChatGPT logins no longer look alike. The login-breaker's account-switch hold, the budget fingerprint and plan-window ownership now see a Codex account switch. `account_kind()` stays the mode only. The label carries no email or token. The first label after the upgrade differs from earlier ones, so the budget fingerprint sees one account change and re-checks any Codex holds once.
