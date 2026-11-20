"""setup-box shared storage: the pieces behind `box.py setup mount|mountcheck <alias>` (see storage.md).

- A network filesystem mounted by hand is gone after the next reboot. `mount` makes it persistent with
  one of two generic methods: an /etc/fstab line with `_netdev,nofail`, or a systemd .mount unit
  ordered After=/Wants= the network and the filesystem's client agent service, when it has one.
- Source, mount point, type and options come from the box's saved answers (the local site file),
  never from this repository; `live` takes the source or type from what is mounted there now.
- The root script runs through `sudo -n`; without passwordless sudo it is printed for the user.
- `mountcheck` verifies now (mountpoint -q, a read, persistence) and records the boot id;
  `--after-reboot` passes only once the box has booted since then and the mount came back.
"""

from __future__ import annotations

import re
import shlex
from typing import Dict, List, Optional

FSTAB = "/etc/fstab"
UNIT_DIR = "/etc/systemd/system"
READ_TIMEOUT_S = 20
BOOT_ID_SCRIPT = "cat /proc/sys/kernel/random/boot_id"   # the ledger's probe reads only this
_SPEC = re.compile(r"^[A-Za-z0-9._:/@=,+-]+$")
_FSTYPE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_UNIT = re.compile(r"^[A-Za-z0-9@._:-]+$")


def valid_mountpoint(v: str) -> bool:
    return bool(_SPEC.match(v)) and v.startswith("/") and v.rstrip("/") != "" and "/../" not in v + "/"


def valid_spec(v: str) -> bool:
    """A source or options value: no spaces, quotes, `%` (a systemd specifier) or backslashes."""
    return bool(_SPEC.match(v)) and not v.startswith("-")


def valid_fstype(v: str) -> bool:
    return bool(_FSTYPE.match(v))


def valid_unit(v: str) -> bool:
    return bool(_UNIT.match(v)) and not v.startswith("-")


def unit_name(where: str) -> str:
    """The .mount unit systemd requires for `where` (as `systemd-escape --path --suffix=mount`)."""
    p = "/".join(s for s in where.split("/") if s)
    if not p:
        return "-.mount"
    out = []
    for i, c in enumerate(p):
        if c == "/":
            out.append("-")
        elif c.isascii() and (c.isalnum() or c in ":_" or c == "." and i > 0):
            out.append(c)
        else:
            out.append("".join(f"\\x{b:02x}" for b in c.encode()))
    return "".join(out) + ".mount"


def agent_unit(agent: Optional[str]) -> Optional[str]:
    if not agent or agent == "none":
        return None
    return agent if "." in agent else agent + ".service"


def mount_options(options: str, fstab: bool = True) -> str:
    """The user's options plus `_netdev` (wait for the network) and, in fstab, `nofail` (boot on without it)."""
    opts = [o for o in (options or "defaults").split(",") if o]
    for need in ("_netdev", "nofail") if fstab else ("_netdev",):
        if need not in opts:
            opts.append(need)
    return ",".join(opts)


def fstab_line(source: str, where: str, fstype: str, options: str) -> str:
    return f"{source} {where} {fstype} {mount_options(options)} 0 0"


def unit_text(source: str, where: str, fstype: str, options: str, agent: Optional[str]) -> str:
    after = " ".join(u for u in ("network-online.target", agent_unit(agent)) if u)
    return "\n".join([
        "[Unit]", f"Description=Shared network storage at {where} (setup-box)",
        f"Wants={after}", f"After={after}", "",
        "[Mount]", f"What={source}", f"Where={where}", f"Type={fstype}",
        f"Options={mount_options(options, fstab=False)}", "",
        "[Install]", "WantedBy=remote-fs.target", ""])


def spec(answers: dict) -> Dict[str, Optional[str]]:
    """The saved storage answers, or {} when the box mounts no shared storage."""
    where = answers.get("storage")
    if not where or where == "none":
        return {}
    return {"where": where, "source": answers.get("storage.source"), "fstype": answers.get("storage.fstype"),
            "options": answers.get("storage.options", "defaults"), "method": answers.get("storage.method"),
            "agent": answers.get("storage.agent")}


def resolve(s: dict, probe: dict) -> "tuple[dict, List[str]]":
    """`s` with `live` source and type taken from what is mounted now; errors when that is not there."""
    out, errs = dict(s), []
    for key, i in (("source", 0), ("fstype", 1)):
        if out.get(key) == "live":
            got = (probe.get("live") or "").split()
            if len(got) < 2:
                errs.append(f"{key} is `live` but nothing is mounted at {s['where']}: mount it by hand once, "
                            f"or save the {key} (`box.py setup save <alias> --set storage.{key}=<value>`)")
            else:
                out[key] = got[i]
    return out, errs


def apply_script(s: dict) -> str:
    """The root script: make `s` persistent and mount it now. Refuses (exit 3) to replace another entry."""
    mp = shlex.quote(s["where"])
    head = ["set -e", f"mp={mp}", 'mkdir -p -- "$mp"']
    if s["method"] == "systemd":
        unit = unit_name(s["where"])
        lines = unit_text(s["source"], s["where"], s["fstype"], s["options"], s.get("agent")).splitlines()
        return "\n".join(head + [
            f"unit={shlex.quote(unit)}; f={UNIT_DIR}/$unit; tmp={UNIT_DIR}/.$unit.setup-box",
            'if findmnt --fstab -n --mountpoint "$mp" >/dev/null 2>&1; then',
            '  echo "an /etc/fstab entry for $mp exists: use the fstab method, or remove that entry first" >&2; exit 3',
            "fi",
            "printf '%s\\n' " + " ".join(shlex.quote(ln) for ln in lines) + ' > "$tmp"',
            'if [ -e "$f" ] && ! cmp -s "$tmp" "$f"; then',
            '  rm -f -- "$tmp"; echo "$f exists with other content; not changed" >&2; exit 3',
            "fi",
            'mv -f -- "$tmp" "$f"',
            "systemctl daemon-reload",
            'systemctl enable --now "$unit"',
            'echo "persistent: $unit enabled"'])
    line = fstab_line(s["source"], s["where"], s["fstype"], s["options"])
    return "\n".join(head + [
        f"line={shlex.quote(line)}",
        f'if findmnt --fstab -n --mountpoint "$mp" >/dev/null 2>&1; then',
        f'  if grep -qxF -- "$line" {FSTAB}; then echo "persistent: the fstab line is already there"; else',
        f'    echo "another {FSTAB} entry for $mp; not changed" >&2; exit 3; fi',
        "else",
        f'  cp -p {FSTAB} "{FSTAB}.setup-box.$(date +%Y%m%d%H%M%S)"',
        f"  printf '%s\\n' \"$line\" >> {FSTAB}",
        '  echo "persistent: fstab line added"',
        "fi",
        "systemctl daemon-reload",
        'mountpoint -q -- "$mp" || mount -- "$mp"'])


def undo_command(alias: str, s: dict) -> str:
    """One line, run from the project box: unmount and remove only what `apply_script` added."""
    mp = shlex.quote(s["where"])
    if s["method"] == "systemd":
        unit = shlex.quote(unit_name(s["where"]))
        root = f"systemctl disable --now {unit}; rm -f -- {UNIT_DIR}/{unit}; systemctl daemon-reload"
    else:
        line = shlex.quote(fstab_line(s["source"], s["where"], s["fstype"], s["options"]))
        new = f"{FSTAB}.setup-box.new"
        root = (f"umount -- {mp}; if grep -vxF -- {line} {FSTAB} > {new} && [ -s {new} ]; then "
                f"cat {new} > {FSTAB}; fi; rm -f -- {new}; systemctl daemon-reload")
    return f"ssh {alias} " + shlex.quote("sudo -n sh -c " + shlex.quote(root))


def probe_script(where: str) -> str:
    """Read-only, as the user: boot id, mounted and a read (each under a timeout, so a hung mount shows as
    one), fstab entry, unit state."""
    unit = shlex.quote(unit_name(where))
    return (
        f"mp={shlex.quote(where)}; "
        'echo "boot_id=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"; '
        f'if timeout {READ_TIMEOUT_S} mountpoint -q -- "$mp"; then echo mounted=yes; '
        f'if timeout {READ_TIMEOUT_S} ls -A -- "$mp" >/dev/null 2>&1; then echo read=yes; else echo read=no; fi; '
        'elif [ "$?" -eq 124 ]; then echo mounted=hung; else echo mounted=no; fi; '
        'echo "live=$(findmnt -rn -o SOURCE,FSTYPE --mountpoint "$mp" 2>/dev/null | head -n1)"; '
        'echo "fstab=$(findmnt --fstab -rn -o SOURCE,FSTYPE,OPTIONS --mountpoint "$mp" 2>/dev/null | head -n1)"; '
        f'echo "unit=$(systemctl is-enabled {unit} 2>/dev/null)"; true'
    )


def read_probe(out: str) -> dict:
    kv = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)
    return {k: v.strip() for k, v in kv.items()}


def judge(s: dict, probe: dict) -> "tuple[List[str], List[str]]":
    """(report lines, problems) for `s` against a probe: mounted, readable, the source wanted, persistent."""
    lines: List[str] = []
    problems: List[str] = []
    where = s["where"]
    if probe.get("mounted") == "hung":
        problems.append("mounted")
        lines.append(f"mounted: `mountpoint -q {where}` took over {READ_TIMEOUT_S}s (a hung mount)")
    elif probe.get("mounted") != "yes":
        problems.append("mounted")
        lines.append(f"mounted: no, nothing is mounted at {where}")
    else:
        live = (probe.get("live") or "").split()
        lines.append(f"mounted: {' '.join(live) or 'yes'} at {where}")
        if s.get("source") not in (None, "live") and live and live[0] != s["source"]:
            problems.append("source")
            lines.append(f"source: {live[0]} is mounted there, not {s['source']}")
        if probe.get("read") == "yes":
            lines.append("read: ok")
        else:
            problems.append("read")
            lines.append(f"read: listing {where} failed or took over {READ_TIMEOUT_S}s")
    if s.get("method") == "systemd":
        state = probe.get("unit") or "missing"
        ok = state == "enabled"
        lines.append(f"persistent: {unit_name(where)} {state}")
    else:
        entry = probe.get("fstab") or ""
        ok = bool(entry)
        lines.append("persistent: " + (f"fstab {entry}" if ok else f"no {FSTAB} entry for {where}"))
        if ok and "_netdev" not in entry.split()[-1].split(","):
            lines.append("warning: the fstab entry has no _netdev, so boot may try it before the network is up")
    if not ok:
        problems.append("persistent")
    return lines, problems


def change_what(alias: str, s: dict) -> str:
    """The ledger entry's text. Its until-probe turns it due once the box rebooted, and the ledger makes a
    probe-only entry due after 7 days anyway: either way the reader runs the check, never the undo."""
    how = "a systemd .mount unit" if s["method"] == "systemd" else f"an {FSTAB} line"
    return (f"setup-box: shared storage at {s['where']} mounted at boot through {how}; keep it. Due (rebooted, or "
            f"7 days up): run `box.py setup mountcheck {alias} --after-reboot` and act on its exit as setup-box "
            "storage.md says; the undo only removes the mount")
