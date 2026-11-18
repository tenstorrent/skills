#!/usr/bin/env python3
"""setup-box helper: check the local site file and run commands on a box over ssh.

Standard library only, Python 3.9+.

Extending (route tasks add their own file; they never need to edit this one):
- Put `box_<name>.py` next to this script. It may define
  `register(sub)`: add subcommands to the top-level argparse subparsers; each parser sets
      `func=<callable(args) -> int>` with `set_defaults`;
  `CHECKS`: {route: callable(alias, entry) -> list of error strings}, run by `site check`
      after the core checks for boxes of that route;
  `KNOWN_KEYS`: extra box-entry keys that `site check` accepts without a warning.
- Reuse `load_site()`, `box_entry()` and `ssh_run()` from here (`import box`).
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import pathlib
import shlex
import subprocess
import sys
from typing import Callable, Dict, List, Optional, Sequence, Union

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_SITE = pathlib.Path("~/.tt-project/boxes.json")
ROUTES = ("bare", "ird", "slurm")
KNOWN_KEYS = {"route", "jump", "workdir_root", "scratch_root", "ird", "slurm",
              "demo_nodes", "excluded_nodes", "mcp", "note"}
SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=15")
# ssh options a site may give for the IRD management host (`ird.mgmt_ssh`), so ird_e2e.sh can reach
# it with no personal ssh config. Host key checking is not among them: it stays on.
MGMT_SSH_KEYS = ("HostName", "User", "Port", "IdentityFile", "IdentitiesOnly", "CertificateFile", "ProxyJump")

CHECKS: Dict[str, List[Callable[[str, dict], List[str]]]] = {}


class SiteError(Exception):
    pass


def site_path(arg: Optional[str] = None) -> pathlib.Path:
    return pathlib.Path(arg or os.environ.get("TTP_BOXES_FILE") or DEFAULT_SITE).expanduser()


def load_site(path: Optional[Union[str, pathlib.Path]] = None) -> dict:
    """The parsed site file; SiteError when it is missing or not JSON."""
    p = site_path(str(path) if path else None)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SiteError(f"no site file at {p} (see site.md)") from None
    except (OSError, ValueError) as e:
        raise SiteError(f"{p}: {e}") from None


WRAPPER_HINT = ('missing version/boxes wrapper: the site file must look like '
                '{"version": 1, "boxes": {"<alias>": {"route": ..., ...}}} (see site.md)')


def missing_wrapper(site) -> bool:
    """A file of bare box entries, without the {"version": 1, "boxes": {...}} wrapper."""
    return isinstance(site, dict) and "boxes" not in site and "version" not in site and \
        any(isinstance(v, dict) and "route" in v for v in site.values())


def box_entry(site: dict, alias: str) -> dict:
    if missing_wrapper(site):
        raise SiteError(WRAPPER_HINT)
    boxes = site.get("boxes") if isinstance(site, dict) else None
    if not isinstance(boxes, dict) or not isinstance(boxes.get(alias), dict):
        raise SiteError(f"no box {alias!r} in the site file")
    return boxes[alias]


def _str(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _str_list(v, nonempty=False) -> bool:
    return isinstance(v, list) and all(_str(x) for x in v) and (bool(v) or not nonempty)


def _abs(v) -> bool:
    return _str(v) and v.startswith(("/", "~", "$"))


def chips_known(part: dict) -> bool:
    """A slurm partition entry carries a measured chip count (whole number > 0)."""
    c = part.get("chips")
    return isinstance(c, int) and not isinstance(c, bool) and c > 0


def check_box(alias: str, e, known=KNOWN_KEYS) -> "tuple[List[str], List[str]]":
    """(errors, warnings) for one box entry."""
    if not isinstance(e, dict):
        return [f"{alias}: entry is not an object"], []
    err: List[str] = []
    warn = [f"{alias}: unknown key {k!r}" for k in sorted(set(e) - set(known))]
    route = e.get("route")
    if route not in ROUTES:
        err.append(f"{alias}: route must be one of {', '.join(ROUTES)}, not {route!r}")
    if not _abs(e.get("workdir_root")):
        err.append(f"{alias}: workdir_root must be an absolute path")
    if "scratch_root" in e and not _abs(e["scratch_root"]):
        err.append(f"{alias}: scratch_root must be an absolute path")
    if "jump" in e and not _str(e["jump"]):
        err.append(f"{alias}: jump must be an ssh alias")
    for k in ("demo_nodes", "excluded_nodes"):
        if k in e and not _str_list(e[k]):
            err.append(f"{alias}: {k} must be a list of node names")
    if "mcp" in e and not (isinstance(e["mcp"], dict) and all(_str(v) for v in e["mcp"].values())):
        err.append(f"{alias}: mcp must map server names to endpoints")
    if route == "ird":
        ird = e.get("ird")
        if not isinstance(ird, dict):
            err.append(f"{alias}: route ird needs an ird object")
        else:
            if not _str(ird.get("mgmt")):
                err.append(f"{alias}: ird.mgmt must be the management host's ssh alias")
            if not _str_list(ird.get("pools"), nonempty=True):
                err.append(f"{alias}: ird.pools must be a non-empty list of pool names")
            ms = ird.get("mgmt_ssh")
            if ms is not None and not (isinstance(ms, dict) and all(
                    k in MGMT_SSH_KEYS and isinstance(v, (str, int)) and not isinstance(v, bool)
                    and str(v).strip() and "\n" not in str(v) for k, v in ms.items())):
                err.append(f"{alias}: ird.mgmt_ssh must map ssh options ({', '.join(MGMT_SSH_KEYS)}) to one-line values")
    if route == "slurm":
        sl = e.get("slurm")
        if not isinstance(sl, dict):
            err.append(f"{alias}: route slurm needs a slurm object")
        else:
            if not _str(sl.get("login")):
                err.append(f"{alias}: slurm.login must be the login node's ssh alias")
            parts = sl.get("partitions")
            if not isinstance(parts, list) or not parts:
                err.append(f"{alias}: slurm.partitions must be a non-empty list")
            else:
                for i, p in enumerate(parts):
                    where = f"{alias}: slurm.partitions[{i}]"
                    if not isinstance(p, dict):
                        err.append(f"{where} is not an object")
                        continue
                    if not _str(p.get("name")):
                        err.append(f"{where}.name is missing")
                    c = p.get("chips")
                    if not chips_known(p):
                        if c is None or c == "unknown":
                            warn.append(f"{where}.chips is not measured yet: the first job on it "
                                        f"reports the count to record (slurm.md)")
                        else:
                            err.append(f"{where}.chips must be a whole number > 0, or \"unknown\"")
                    if not _str(p.get("board")):
                        err.append(f"{where}.board is missing")
                    if "exclusive" in p and not isinstance(p["exclusive"], bool):
                        err.append(f"{where}.exclusive must be true or false")
            for k in ("container_run", "state_dir"):
                v = sl.get(k)
                if v is not None and not (_str(v) and "\n" not in v):
                    err.append(f"{alias}: slurm.{k} must be a one-line string")
            sd = sl.get("state_dir")
            if isinstance(sd, str) and sd.strip() and not sd.startswith(("/", "~/", "$")):
                err.append(f"{alias}: slurm.state_dir must be an absolute path on shared storage")
    if not err:
        for fn in CHECKS.get(route, []):
            err.extend(fn(alias, e))
    return err, warn


def check_site(site, known=KNOWN_KEYS) -> "tuple[List[str], List[str]]":
    """(errors, warnings) for the whole site file."""
    if not isinstance(site, dict):
        return ["the site file is not a JSON object"], []
    if missing_wrapper(site):
        return [WRAPPER_HINT], []
    err: List[str] = []
    warn: List[str] = []
    if site.get("version") != 1:
        err.append(f"version must be 1, not {site.get('version')!r}")
    if "mcp" in site and not (isinstance(site["mcp"], dict) and all(_str(v) for v in site["mcp"].values())):
        err.append("mcp must map server names to endpoints")
    boxes = site.get("boxes")
    if not isinstance(boxes, dict) or not boxes:
        err.append("boxes must be a non-empty object keyed by ttp machines alias")
        return err, warn
    for alias, e in boxes.items():
        if not _str(alias) or any(c.isspace() for c in alias):
            err.append(f"box alias {alias!r} must be one word")
        e_err, e_warn = check_box(alias, e, known)
        err += e_err
        warn += e_warn
    return err, warn


def ssh_cmd() -> List[str]:
    """`ssh`, with `-F $TTP_SSH_CONFIG` when set: ird_e2e.sh runs every ssh under its own config."""
    cfg = os.environ.get("TTP_SSH_CONFIG")
    return ["ssh", "-F", cfg] if cfg else ["ssh"]


def ssh_run(alias: str, cmd: Union[str, Sequence[str]], *, timeout: float = 120,
            input: Optional[str] = None, opts: Sequence[str] = ()) -> subprocess.CompletedProcess:
    """Run `cmd` on `alias` over ssh, non-interactively; never raises on a non-zero exit.

    A list is quoted for the remote shell; a string runs as written. Exit 255 is ssh itself
    failing (unreachable, auth). A timeout returns code 124 with what was captured.
    """
    remote = cmd if isinstance(cmd, str) else shlex.join(cmd)
    argv = [*ssh_cmd(), *SSH_OPTS, *opts, alias, remote]
    try:
        return subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return subprocess.CompletedProcess(argv, 124, out, err + f"\ntimed out after {timeout:g}s")


def cmd_site_check(args) -> int:
    try:
        site = load_site(args.site)
    except SiteError as e:
        print(f"error: {e}")
        return 1
    err, warn = check_site(site, KNOWN_KEYS | args.extra_keys)
    for w in warn:
        print(f"warning: {w}")
    for e in err:
        print(f"error: {e}")
    if err:
        return 1
    for alias, e in site["boxes"].items():
        print(f"ok {alias} route={e['route']}")
    return 0


def cmd_site_show(args) -> int:
    try:
        print(json.dumps(box_entry(load_site(args.site), args.alias), indent=2))
    except SiteError as e:
        print(f"error: {e}")
        return 1
    return 0


def load_extensions(sub) -> set:
    """Import box_*.py next to this script; returns the extra keys they accept."""
    extra: set = set()
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    for path in sorted(HERE.glob("box_*.py")):
        mod = importlib.import_module(path.stem)
        if hasattr(mod, "register"):
            mod.register(sub)
        for route, fn in getattr(mod, "CHECKS", {}).items():
            if fn not in CHECKS.setdefault(route, []):
                CHECKS[route].append(fn)
        extra |= set(getattr(mod, "KNOWN_KEYS", ()))
    return extra


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="box.py", description="setup-box helper")
    ap.add_argument("--site", help="site file (default $TTP_BOXES_FILE or ~/.tt-project/boxes.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    site = sub.add_parser("site", help="the local site file").add_subparsers(dest="site_cmd", required=True)
    site.add_parser("check", help="validate the site file").set_defaults(func=cmd_site_check)
    s = site.add_parser("show", help="print one box's entry")
    s.add_argument("alias")
    s.set_defaults(func=cmd_site_show)
    ap.set_defaults(extra_keys=load_extensions(sub))
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
