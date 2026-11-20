---
"tt-project": patch
---

`tt-project`: `ttp setup`, and so every upgrade, keeps interactive Claude Code sessions on the release the daemons and `ttp` run. The installed release (`~/.tt-project/lib/current`) is now also a one-plugin `tenstorrent-skills` marketplace. A `tenstorrent-skills` directory marketplace registered in Claude Code's settings or known marketplaces that holds an older tt-project (such as an installer clone that upgrades never touch) is repointed there, after a backup of each file it edits, and the plugin cache is refreshed with `claude plugin marketplace update` and `claude plugin update`. A plugin cache behind that marketplace that is older than the release is refreshed the same way. Other marketplaces are left untouched, as is a `tenstorrent-skills` registration that also serves another installed or enabled plugin (setup says to rerun the installer). It never installs the plugin on its own, and a second run changes nothing.
