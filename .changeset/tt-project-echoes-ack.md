---
"tt-project": patch
---

`tt-project`:

- `ttp echoes --ack <retirement> <place>` acknowledges one retired-rule echo match as a false positive (for example code that uses a field name the retired text also holds). It is stored in the project's state with a hash of the matched line; while that line is unchanged the match is no fix order, and `ttp echoes` and the digest still list or count it as acknowledged. A changed line lists it again. The strong/weak phrase rules are unchanged.
