---
"tt-project": patch
---

`tt-project`: the reboot notice's initiator reads a journal `COMMAND=` with the same decider as the `host_reboot` rule (`systemctl soft-reboot` and `init 6` now count; `shutdown -c` does not), and one heredoc reader in `hook` serves the `host_reboot` and kill guards (`cat <<EOF | ssh box bash` is read as run on the box). Two duplicate stop-command tables and a duplicate heredoc lookup are deleted.
