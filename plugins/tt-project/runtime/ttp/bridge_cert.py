# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Offline mode for `ttp bridge`: short-lived ssh user certificates (`ttp bridge cert`).

Off unless the bridge config has a "cert" section. Each box listed there keeps its own key, made on
the box (`renew --make-key`), in a tool-owned folder (`key_dir`, mode 0700). While this computer is
online it signs that key's public half and puts the certificate back on the box:

- the signer is pluggable: `agent-ca` signs with a CA key held in this computer's ssh agent
  (`ssh-keygen -s <ca.pub> -U`; the tool never reads or writes a private key file), `command` hands
  the public key to an outside signer (a central CA) on stdin and reads the certificate on stdout;
- a certificate is valid `validity_hours` (default 72) for the configured principals, bound to the
  box's address (`source-address`), with every extension cleared (no agent, port or X11
  forwarding, no user rc) except `permit-pty` when `pty` is set. A signed certificate that is
  broader than asked, for another key or from another CA is refused, not installed;
- `renew` signs again once less than half the validity is left; `schedule` runs it every 30 minutes
  as a user service on this computer.

The box's ssh config block (bridge.render_block) reaches each `hosts` entry directly with the
box's key and certificate only while the bridge's agent link is down and the certificate is still
valid (a small `valid` script the renewal writes); after that the login fails, with a message
saying so. Targets trust the signer per user: `ttp bridge cert trust --print` prints the
authorized_keys line; nothing edits authorized_keys.
"""
from __future__ import annotations

import os
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .project import HOME_DIR, durable_write

KEY_DIR = "~/.config/tt-project/bridge-cert"
KEY = "id_ed25519"
LABEL = "com.tt-project.bridge-cert.renew"
RENEW_EVERY_S = 1800
ADDR = re.compile(r"^[0-9A-Fa-f.:/]{2,64}$")
PUB = re.compile(r"^(ssh-[a-z0-9-]+|ecdsa-sha2-[a-z0-9-]+|sk-[a-z0-9@.-]+) [A-Za-z0-9+/=]+( .*)?$")


def parse(data, bridge_cfg: dict) -> dict:
    """The checked "cert" section. Every box in it is a box of the bridge config."""
    from .bridge import BadConfig, _name
    if not isinstance(data, dict):
        raise BadConfig("cert: an object")
    s = data.get("signer")
    if not isinstance(s, dict) or s.get("kind") not in ("agent-ca", "command"):
        raise BadConfig("cert.signer: {\"kind\": \"agent-ca\" or \"command\", \"ca_public_key\": <.pub file>, ...}")
    ca = s.get("ca_public_key")
    if not isinstance(ca, str) or not ca.endswith(".pub"):
        raise BadConfig("cert.signer.ca_public_key: the CA's public key file (.pub); its private key never goes in the config")
    signer = {"kind": s["kind"], "ca_public_key": ca, "agent": s.get("agent") or "env", "command": []}
    if s["kind"] == "command":
        cmd = s.get("command")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(w, str) and w for w in cmd):
            raise BadConfig("cert.signer.command: the signer's argv as a list of strings")
        signer["command"] = cmd
    hours = data.get("validity_hours", 72)
    if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 168:
        raise BadConfig(f"cert.validity_hours: 1 to 168, not {hours!r}")
    principals = data.get("principals")
    if not isinstance(principals, list) or not principals:
        raise BadConfig("cert.principals: the target user names the certificate may log in as")
    key_dir = data.get("key_dir", KEY_DIR)
    if not isinstance(key_dir, str) or not key_dir.startswith("~/") or re.search(r"[^A-Za-z0-9_./~-]", key_dir) or ".." in key_dir:
        raise BadConfig(f"cert.key_dir {key_dir!r}: a plain path under the box account's home, starting with '~/'")
    boxes = data.get("boxes")
    if not isinstance(boxes, dict) or not boxes:
        raise BadConfig("cert.boxes: one entry per box")
    out = {"signer": signer, "validity_hours": hours, "principals": [_name(p, "cert principal") for p in principals],
           "pty": bool(data.get("pty", True)), "key_dir": key_dir.rstrip("/"), "boxes": {}}
    for box, b in boxes.items():
        if box not in bridge_cfg["boxes"]:
            raise BadConfig(f"cert.boxes: {box} is not a box of the bridge")
        if not isinstance(b, dict) or not isinstance(b.get("source_address"), str) or not ADDR.match(b["source_address"]):
            raise BadConfig(f"cert box {box}: source_address is the box's address (or CIDR) as its targets see it")
        hosts = []
        for h in b.get("hosts") or []:
            if not isinstance(h, dict):
                raise BadConfig(f"cert box {box}: each host is an object")
            hn = _name(h.get("name"), f"cert box {box}: host")
            hosts.append({"name": hn, "aliases": [_name(x, f"host {hn}: alias") for x in h.get("aliases") or []],
                          "target": _name(h.get("target") or hn, f"host {hn}: target"),
                          "user": _name(h["user"], f"host {hn}: user") if h.get("user") else ""})
        if not hosts:
            raise BadConfig(f"cert box {box}: hosts: the hosts the box reaches directly with its certificate")
        out["boxes"][box] = {"source_address": b["source_address"], "hosts": hosts}
    return out


def paths(c: dict) -> dict:
    d = c["key_dir"]
    return {"dir": d, "key": f"{d}/{KEY}", "pub": f"{d}/{KEY}.pub", "cert": f"{d}/{KEY}-cert.pub", "valid": f"{d}/valid"}


def block_lines(cfg: dict, box: str) -> tuple[list[str], set[str]]:
    """The offline entries of the box's block and the key-file lines they may hold. Each host goes
    direct with the box's key and certificate only while its bridge link does not answer and the
    certificate is valid; once the certificate has ended, a second entry turns every login method
    off for it, so it fails rather than trying other keys. They come first, so they win."""
    c = cfg.get("cert")
    if not c or box not in c["boxes"]:
        return [], set()
    p = paths(c)
    bridged = {n: an for an, a in cfg["boxes"][box]["agents"].items()
               for h in a["forward"] + a["direct"] for n in [h["name"], *h["aliases"]]}
    keyalias = {n: h["host_key_alias"] for a in cfg["boxes"][box]["agents"].values()
                for h in a["forward"] for n in [h["name"], *h["aliases"]]}
    allowed = {f"  IdentityFile {p['key']}", f"  CertificateFile {p['cert']}"}
    lines = ["", "# Offline: while the link to the workstation is down, these go direct with this box's own key and a",
             "# short-lived certificate the workstation renews; once it ends, they fail."]
    for h in c["boxes"][box]["hosts"]:
        names = ",".join([h["name"], *h["aliases"]])
        # the link is down when its agent does not answer (ssh-add exits 2), not only when its socket is gone
        down = (f' !exec "SSH_AUTH_SOCK=$HOME/{link_path(cfg, bridged[h["name"]])} ssh-add -l >/dev/null 2>&1; [ $? -lt 2 ]"'
                if h["name"] in bridged else "")
        lines += ["", f"Match originalhost {names}{down} exec \"sh {p['valid']}\"", f"  HostName {h['target']}",
                  "  Port 22", "  IdentityAgent none", "  IdentitiesOnly yes", f"  IdentityFile {p['key']}",
                  f"  CertificateFile {p['cert']}"]
        if h["name"] in keyalias:
            lines.append(f"  HostKeyAlias {keyalias[h['name']]}")
        if h["user"]:
            lines.append(f"  User {h['user']}")
        lines += ["", f"Match originalhost {names}{down} !exec \"sh {p['valid']} quiet\"", "  IdentityAgent none",
                  "  IdentitiesOnly yes", "  PubkeyAuthentication no", "  PasswordAuthentication no",
                  "  KbdInteractiveAuthentication no", "  HostbasedAuthentication no", "  GSSAPIAuthentication no",
                  "  BatchMode yes"]
    return lines, allowed


def link_path(cfg: dict, agent: str) -> str:
    """The agent link's socket below the box account's home."""
    return f"{cfg['socket_dir'][2:]}/{agent}.sock"


def _run(argv: list[str], timeout: int = 60, inp: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=inp, env=env)
    except (subprocess.TimeoutExpired, OSError) as e:
        return subprocess.CompletedProcess(argv, 124, "", str(e))


def fingerprint(pub: str) -> str:
    """The SHA256 fingerprint of a public key or certificate line ('' if ssh-keygen cannot read it)."""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "k.pub"
        f.write_text(pub.strip() + "\n")
        r = _run(["ssh-keygen", "-l", "-f", str(f)])
    m = re.search(r"SHA256:[A-Za-z0-9+/=]+", r.stdout)
    return m.group(0) if r.returncode == 0 and m else ""


def inspect(cert: str) -> dict:
    """`ssh-keygen -L` of a certificate line: {user, key, ca, id, from, to (epochs; to=0: forever), principals,
    options {name: value}, extensions}; {} when it is not a certificate."""
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "c-cert.pub"
        f.write_text(cert.strip() + "\n")
        r = _run(["ssh-keygen", "-L", "-f", str(f)])
    if r.returncode != 0:
        return {}
    out: dict = {"user": False, "key": "", "ca": "", "id": "", "from": 0, "to": 0, "principals": [],
                 "options": {}, "extensions": []}
    section = ""
    for ln in r.stdout.splitlines():
        s = ln.strip()
        if s.startswith("Type:"):
            out["user"] = "user certificate" in s
        elif s.startswith("Public key:"):
            m = re.search(r"SHA256:[A-Za-z0-9+/=]+", s)
            out["key"] = m.group(0) if m else ""
        elif s.startswith("Signing CA:"):
            m = re.search(r"SHA256:[A-Za-z0-9+/=]+", s)
            out["ca"] = m.group(0) if m else ""
        elif s.startswith("Key ID:"):
            out["id"] = s.split(":", 1)[1].strip().strip('"')
        elif s.startswith("Valid:"):
            m = re.search(r"from (\S+) to (\S+)", s)
            if m:
                out["from"], out["to"] = (int(time.mktime(time.strptime(t, "%Y-%m-%dT%H:%M:%S"))) for t in m.groups())
        elif s.split(":", 1)[0] in ("Principals", "Critical Options", "Extensions"):
            section = s.split(":", 1)[0]
        elif s and section and s != "(none)":
            if section == "Principals":
                out["principals"].append(s)
            elif section == "Critical Options":
                k, _, v = s.partition(" ")
                out["options"][k] = v.strip()
            else:
                out["extensions"].append(s)
    return out


def check(cert: str, c: dict, box: str, box_pub: str, now: float | None = None) -> tuple[dict, str]:
    """(inspect result, '' or why the certificate is refused): it must be a user certificate for the
    box's key, from the configured CA, for configured principals only, bound to the box's address,
    with no extension but permit-pty and no longer than the configured validity."""
    now = time.time() if now is None else now
    i = inspect(cert)
    if not i or not i["user"]:
        return i, "not a user certificate"
    ca = Path(c["signer"]["ca_public_key"]).expanduser()
    want = {"key": fingerprint(box_pub), "ca": fingerprint(ca.read_text()) if ca.is_file() else ""}
    for k, what in (("key", "the box's key"), ("ca", "the configured CA")):
        if not want[k] or i[k] != want[k]:
            return i, f"not signed for {what} ({i[k] or 'unknown'} is not {want[k] or 'readable'})"
    if not i["principals"] or set(i["principals"]) - set(c["principals"]):
        return i, f"principals {i['principals']} are not within {c['principals']}"
    if i["options"].get("source-address") != c["boxes"][box]["source_address"]:
        return i, f"source-address {i['options'].get('source-address')!r} is not {c['boxes'][box]['source_address']!r}"
    extra = set(i["extensions"]) - ({"permit-pty"} if c["pty"] else set())
    if extra or set(i["options"]) - {"source-address"}:
        return i, f"it grants more than asked: {sorted(extra | (set(i['options']) - {'source-address'}))}"
    if not i["to"] or i["to"] - now > c["validity_hours"] * 3600 + 600 or i["to"] <= now:
        return i, "its validity is not within the configured hours from now"
    return i, ""


def sign(c: dict, box: str, box_pub: str) -> str:
    """The certificate line for box_pub, from the configured signer (raises RuntimeError)."""
    mins = c["validity_hours"] * 60
    key_id = f"ttp-bridge:{box}:{time.strftime('%Y%m%dT%H%M%S')}"
    s = c["signer"]
    if s["kind"] == "command":
        env = {**os.environ, "TTP_CERT_KEY_ID": key_id, "TTP_CERT_PRINCIPALS": ",".join(c["principals"]),
               "TTP_CERT_VALIDITY_MINUTES": str(mins), "TTP_CERT_SOURCE_ADDRESS": c["boxes"][box]["source_address"],
               "TTP_CERT_PERMIT_PTY": "1" if c["pty"] else "0"}
        r = _run(s["command"], timeout=120, inp=box_pub.strip() + "\n", env=env)
        if r.returncode != 0:
            raise RuntimeError(f"the signer command failed (exit {r.returncode}): {r.stderr.strip()[-200:]}")
        return r.stdout.strip()
    env = dict(os.environ)
    if s["agent"] != "env":
        env["SSH_AUTH_SOCK"] = str(Path(s["agent"]).expanduser())
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "box.pub"
        f.write_text(box_pub.strip() + "\n")
        argv = ["ssh-keygen", "-q", "-s", str(Path(s["ca_public_key"]).expanduser()), "-U", "-I", key_id,
                "-n", ",".join(c["principals"]), "-V", f"-5m:+{mins - 5}m", "-O", "clear",
                "-O", f"source-address={c['boxes'][box]['source_address']}"]
        argv += ["-O", "permit-pty"] if c["pty"] else []
        r = _run(argv + [str(f)], env=env)
        out = Path(d) / "box-cert.pub"
        if r.returncode != 0 or not out.is_file():
            raise RuntimeError(f"ssh-keygen could not sign (is the CA key loaded in the agent?): {r.stderr.strip()[-200:]}")
        return out.read_text().strip()


SEP = "---ttp-bridge-cert---"


def read_box(box: str, c: dict) -> tuple[str, str]:
    """(the box key's public line, its certificate line); '' for a missing one. RuntimeError if unreachable."""
    from .bridge import _box, _remote
    p = paths(c)
    r = _box(box, f"cat {_remote(p['pub'])} 2>/dev/null; echo {SEP}; cat {_remote(p['cert'])} 2>/dev/null; true")
    if r.returncode != 0 or SEP not in r.stdout:
        raise RuntimeError(f"{box} unreachable ({r.stderr.strip()[-160:]})")
    pub, _, cert = r.stdout.partition(SEP)
    return pub.strip(), cert.strip()


def valid_script(to: int, box: str) -> str:
    when = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(to))
    return ("#!/bin/sh\n# ttp bridge: exit 0 while this box's short-lived certificate is valid (written at each renewal)\n"
            f'[ "$(date +%s)" -lt {int(to)} ] && exit 0\n[ "$1" = quiet ] && exit 1\n'
            f'echo "ttp bridge: the short-lived ssh certificate of {box} ended {when}; logins that need it fail until '
            'the workstation is online again and renews it" >&2\nexit 1\n')


def install(box: str, c: dict, cert: str, to: int) -> None:
    from .bridge import _box, _remote
    p = paths(c)
    d, v = _remote(p["dir"]), _remote(p["valid"])
    script = (f"set -e; umask 077; d={d}; [ -f {_remote(p['key'])} ] || {{ echo 'no box key' >&2; exit 4; }}; "
              f'cat > "$d/cert.tmp"; mv "$d/cert.tmp" {_remote(p["cert"])}; '
              f"cat > \"$d/valid.tmp\" <<'TTPEOF'\n{valid_script(to, box)}TTPEOF\nmv \"$d/valid.tmp\" {v}")
    r = _box(box, script, inp=cert.strip() + "\n")
    if r.returncode != 0:
        raise RuntimeError(f"could not put the certificate on {box}: {r.stderr.strip()[-200:]}")


def make_key(box: str, c: dict) -> str:
    """Make the box's own key on the box (once; the private key never leaves it)."""
    from .bridge import _box, _remote
    p = paths(c)
    r = _box(box, f"set -e; umask 077; mkdir -p {_remote(p['dir'])}; chmod 700 {_remote(p['dir'])}; "
                  f"[ -e {_remote(p['key'])} ] || ssh-keygen -q -t ed25519 -N '' -C 'ttp bridge box key' -f {_remote(p['key'])}; "
                  f"cat {_remote(p['pub'])}")
    if r.returncode != 0 or not PUB.match(r.stdout.strip()):
        raise RuntimeError(f"could not make {box}'s key: {r.stderr.strip()[-200:]}")
    return r.stdout.strip()


def when(t: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(t)) if t else "never"


def renew(cfg: dict, boxes: list[str], force: bool = False, make: bool = False) -> tuple[list[str], int]:
    """Sign each box's key again once less than half its validity is left (or force). rc 1 on any failure."""
    c = cfg["cert"]
    lines, bad = [], 0
    for box in boxes:
        try:
            pub, cert = read_box(box, c)
            if not pub:
                if not make:
                    raise RuntimeError("no box key yet (`ttp bridge cert renew --make-key` makes it on the box)")
                pub = make_key(box, c)
            i, why = check(cert, c, box, pub) if cert else ({}, "none yet")
            if not why and not force and i["to"] - time.time() > c["validity_hours"] * 1800:
                lines.append(f"{box}: current, valid until {when(i['to'])}")
                continue
            new = sign(c, box, pub)
            i, why = check(new, c, box, pub)
            if why:
                raise RuntimeError(f"refusing the signed certificate: {why}")
            install(box, c, new, i["to"])
            lines.append(f"{box}: renewed, valid until {when(i['to'])}")
        except (RuntimeError, OSError) as e:
            bad += 1
            lines.append(f"{box}: {e}")
    return lines, 1 if bad else 0


def status(cfg: dict, boxes: list[str]) -> tuple[list[str], int]:
    c = cfg["cert"]
    lines, bad = [], 0
    for box in boxes:
        try:
            pub, cert = read_box(box, c)
        except RuntimeError as e:
            lines.append(f"{box}: {e}")
            bad += 1
            continue
        if not pub or not cert:
            lines.append(f"{box}: no {'key' if not pub else 'certificate'} on the box")
            bad += 1
            continue
        i, why = check(cert, c, box, pub)
        ok = not why or why.startswith("its validity")
        live = i.get("to", 0) > time.time()
        bad += not (ok and live)
        lines.append(f"{box}: {'OK' if ok and live else 'EXPIRED' if ok else 'BAD'} valid until {when(i.get('to', 0))}; "
                     f"source {i.get('options', {}).get('source-address', '?')}; principals {','.join(i.get('principals', []))}; "
                     f"signer {c['signer']['kind']} ({i.get('ca', '?')})" + (f"; {why}" if why and not ok else ""))
    return lines, 1 if bad else 0


def remove(cfg: dict, boxes: list[str]) -> list[str]:
    """Delete each box's key, certificate and validity script (its offline logins then fail)."""
    from .bridge import _box, _remote
    p = paths(cfg["cert"])
    out = []
    for box in boxes:
        r = _box(box, "rm -f " + " ".join(_remote(p[k]) for k in ("cert", "valid", "key", "pub"))
                 + f"; rmdir {_remote(p['dir'])} 2>/dev/null; true")
        out.append(f"{box}: {'key and certificate removed' if r.returncode == 0 else 'could not remove: ' + r.stderr.strip()[-160:]}")
    return out


def trust_line(cfg: dict) -> str:
    """The line a target user adds to their own authorized_keys to accept this signer's certificates."""
    c = cfg["cert"]
    ca = Path(c["signer"]["ca_public_key"]).expanduser().read_text().strip().splitlines()
    if len(ca) != 1 or not PUB.match(ca[0]):
        raise RuntimeError(f"{c['signer']['ca_public_key']} is not one public key line")
    kind, b64 = ca[0].split()[:2]
    return (f'cert-authority,principals="{",".join(c["principals"])}",no-agent-forwarding,no-port-forwarding,'
            f"no-X11-forwarding,no-user-rc {kind} {b64} ttp-bridge-ca")


def schedule_argv(config: Path | None) -> list[str]:
    return [sys.executable, "-m", "ttp", "bridge", "cert", "renew", *(["--config", str(config)] if config else [])]


def schedule(config: Path | None, platform: str | None = None) -> str:
    """Run `renew` every 30 minutes as a user service on this computer."""
    from .bridge import _run as brun
    platform = platform or sys.platform
    argv = schedule_argv(config)
    env = {"PYTHONPATH": str(Path(__file__).resolve().parent.parent), "HOME": str(Path.home()),
           "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    if platform == "darwin":
        f = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        log = str(HOME_DIR / "bridge-cert.log")
        f.parent.mkdir(parents=True, exist_ok=True)
        durable_write(f, plistlib.dumps({"Label": LABEL, "ProgramArguments": argv, "EnvironmentVariables": env,
                                         "StartInterval": RENEW_EVERY_S, "RunAtLoad": True, "ProcessType": "Background",
                                         "StandardOutPath": log, "StandardErrorPath": log}))
        brun(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"])
        r = brun(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(f)])
        return "renewal scheduled (launchd)" if r.returncode == 0 else f"launchd refused it: {r.stderr.strip()[:200]}"
    from . import hoststate
    from .tunnel import _unit_escape
    d = hoststate.unit_dir()
    d.mkdir(parents=True, exist_ok=True)
    envs = "".join(f"Environment={_unit_escape(k + '=' + v)}\n" for k, v in env.items())
    durable_write(d / f"{LABEL}.service", "[Unit]\nDescription=tt-project: renew the bridge's short-lived certificates\n\n"
                  f"[Service]\nType=oneshot\n{envs}ExecStart={_unit_escape(' '.join(shlex.quote(a) for a in argv))}\n")
    durable_write(d / f"{LABEL}.timer", f"[Timer]\nOnBootSec=2min\nOnUnitActiveSec={RENEW_EVERY_S}s\n\n[Install]\nWantedBy=timers.target\n")
    brun(["systemctl", "--user", "daemon-reload"])
    r = brun(["systemctl", "--user", "enable", "--now", "--", f"{LABEL}.timer"])
    return "renewal scheduled (systemd timer)" if r.returncode == 0 else f"systemd refused it: {r.stderr.strip()[:200]}"
