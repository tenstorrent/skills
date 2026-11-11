# Granted settings

The daemon (runtime `grants.py`) no longer reads the charter's wording to decide what a user granted. The coordinator applies a granted delivery setting itself with `config_set`, in the turn that carries the user's word and when it records the charter change; the setter record keeps who set it.
The daemon keeps one model-free check: for `delivery.push_allowed`, `delivery.allow_protected_push_branch` and `delivery.auto_merge_repos`, a setting whose last recorded setter was the user but whose value changed since is one keyed `grant_mismatch` event for the coordinator (once per setting and recorded set), never an ask. The daemon never writes these settings; the coordinator restores one only on the user's word.
