# Host Questions

D1, D2, D4, D5, D8, D13. Run the whole block in one Bash call, then fill
each row from the labelled section.

## Commands

```bash
echo "==D1"; nproc; grep MemTotal /proc/meminfo; df -h / | tail -1
. /etc/os-release; echo "$PRETTY_NAME"; lscpu | grep -m1 'Model name'
echo "==D2"; cat /sys/module/tenstorrent/version 2>&1; dkms status 2>/dev/null | grep tenstorrent
echo "==D4a"; for d in /sys/kernel/mm/hugepages/*/; do echo "$(basename $d) nr=$(cat $d/nr_hugepages) free=$(cat $d/free_hugepages)"; done
mount -t hugetlbfs
echo "==D4b"; docker --version; docker info --format '{{.ServerVersion}} {{.Driver}} root={{.DockerRootDir}}'
docker ps -a --format '{{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Ports}}\t{{.Image}}'
echo "==D13"; for c in $(docker ps -aq); do docker inspect -f '{{.Name}} {{.State.Status}} {{range .HostConfig.Devices}}{{.PathOnHost}} {{end}}' $c; done | grep tenstorrent
fuser -v /dev/tenstorrent/* 2>&1; echo "fuser rc=$?"
echo "==D5"; echo "HF_HOME=${HF_HOME:-unset} HF_TOKEN=${HF_TOKEN:+set}"
H=${HF_HOME:-$HOME/.cache/huggingface}; ls "$H/hub" 2>/dev/null | grep '^models--'; du -sh "$H" 2>/dev/null
echo "==D8"; ss -ltnp | grep -E ':8000\b' || echo "port 8000 free"
```

## Interpretation

| ID | Question | Read | Answer format |
|---|---|---|---|
| D1 | Host facts | nproc; MemTotal kB ÷ 1048576; df Avail; PRETTY_NAME; Model name | `<n> cores, <n> GB RAM, <n> free, <distro>, <cpu>` |
| D2 | Current driver | `/sys/module/tenstorrent/version` | `TT-KMD <ver>`. File missing → `TT-KMD not loaded` |
| D4 | Hugepages + docker | sysfs `hugepages-1048576kB` nr/free; hugetlbfs mount with `pagesize=1024M`; docker version; container list | `<free> of <nr> 1 GB pages free at <mount>; docker <ver>, <n> containers (<n> running)` |
| D5 | HF cache | `models--*` dirs under `$HF_HOME/hub` (default `~/.cache/huggingface/hub`) | `yes: <n> models, <size>` or `no` |
| D8 | Port 8000 | `ss -ltnp` match | `free` or `held by <process> (pid <n>)` |
| D13 | Device holders | fuser output plus containers whose `HostConfig.Devices` includes `/dev/tenstorrent` | `free` or `<pid/cmd>; container <name> (<status>)` |

## Rules

- D4: the 1 GB pool is the one that matters for TT. `/proc/meminfo`
  `HugePages_Total` reports the default size (2 MB) and reads 0 on a
  correctly configured host. NEVER answer D4 from it.
- D4: if `hugepages-1048576kB` free < nr, list what holds them (D13 containers first).
- D5: `$HF_TOKEN` presence is reported in D5's evidence, never its value.
- D8: `ss` needs no root to list listeners; `-p` may omit process names for other users' sockets. Then report `held (owner not visible)`.
- D13: an **exited** container that lists `/dev/tenstorrent` is not a holder. Record it — D14 in `boards.md` consumes it.
