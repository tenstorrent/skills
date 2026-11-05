---
"tt-project": patch
---

`tt-project`: the coordinator's `config_set` and the web app's settings now treat `''` as removing a project key, so its default applies, as `ttp config <project> KEY ''` does, and refuse a value whose JSON type does not match a list or object key, writing nothing. Who may change a key is unchanged: outside the user's turn, removing a user-only key is refused unless its default is the off side.
