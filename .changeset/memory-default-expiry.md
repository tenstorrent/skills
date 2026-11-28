---
"tt-project": patch
---

Decision, fact and preference memory entries written without an end now expire after `memory.default_expiry_days` (default 30; 0 off); restrictions, resources, settled areas and standing entries never do. `ttp memory <name> --renew <entry> [--days N]` renews one, and an entry a hold, decision or digest line cites by name (or a merged entry that replaces it) is renewed by itself. The digest lists entries expiring within 3 days and, daily, Restrictions and standing entries unused for 30 days (never retired); older entries without an end get one (at least a week out).
