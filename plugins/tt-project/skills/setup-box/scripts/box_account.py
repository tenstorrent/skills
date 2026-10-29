"""setup-box: the user's own account on a box, `box.py account ids|probe|create|keys|migrate|entry`.

A box is set up in the user's own account, not in the shared admin or site account through which
access was first given (`account.md`).

- `ids` prints the user's uid and gid: from this machine, from another machine (`id` over ssh), or
  from the owner of a path on the box (the user's files on shared storage).
- `probe` is read-only, through the admin login: whether the account and its home exist (checked as
  root: a non-root test on another user's home fails silently), the admin's sudo, the docker group,
  and who owns the uid and gid.
- `create` prints the decision and the root script that creates or completes the account: same
  uid/gid, home, login shell, a sudoers.d drop-in only when the admin login has sudo (checked with
  `visudo -cf` before it goes in place), the docker group if there is one, authorized_keys, linger.
  It never changes another account. Dry run by default; `--run` runs it with `sudo -n`.
- `keys` installs public keys into authorized_keys as the user (their own login already works).
- `migrate` moves work done in the admin account into the user's home with sudo: `mv`, then
  `chown`; never a delete. It lists the venvs and build trees inside that must be rebuilt.
- `entry` writes `user`, `admin` and a `workdir_root` under the user's home into the site entry.
"""

from __future__ import annotations

import getpass
import glob
import grp
import json
import os
import pathlib
import pwd
import re
import shlex
from typing import Dict, List, Optional

import box

KNOWN_KEYS = {"user", "admin"}
LOGIN_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
KEY_RE = re.compile(r"^(ssh-|ecdsa-|sk-)[A-Za-z0-9@._-]+ [A-Za-z0-9+/=]+( [^\n]*)?$")
SUDOERS_DIR = "/etc/sudoers.d"
DEFAULT_SHELL = "/bin/bash"


def valid_login(name) -> bool:
    return isinstance(name, str) and bool(LOGIN_RE.match(name))


def local_user() -> str:
    """The user name here: the default login on the box (the same name as on the user's other machines)."""
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return getpass.getuser()


def local_ids() -> dict:
    gid = os.getgid()
    try:
        group = grp.getgrgid(gid).gr_name
    except KeyError:
        group = local_user()
    return {"uid": os.getuid(), "gid": gid, "group": group, "from": "this machine"}


def parse_ids(out: str, where: str) -> dict:
    """`<uid> <gid> [<group>]` (the first line that has them) as ids; ValueError otherwise."""
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            group = parts[2] if len(parts) > 2 and parts[2] != "UNKNOWN" else ""
            return {"uid": int(parts[0]), "gid": int(parts[1]), "group": group, "from": where}
    raise ValueError(f"no uid and gid in the answer from {where}")


def ids_command(source: str) -> Optional[str]:
    """The remote command for a source: an ssh alias runs `id`; an absolute path is stat-ed on the box
    (plainly first: root may be squashed on a network mount; then as root)."""
    if source.startswith("/"):
        p = shlex.quote(source)
        return f"stat -c '%u %g %G' -- {p} 2>/dev/null || sudo -n stat -c '%u %g %G' -- {p}"
    return 'echo "$(id -u) $(id -g) $(id -gn)"'


def discover_ids(source: str, via: Optional[str] = None, run=None) -> dict:
    """uid/gid from `here`, another machine's ssh alias, or a path on the box (read through `via`)."""
    run = run or box.ssh_run
    if source in ("", "here"):
        return local_ids()
    if source.startswith("/"):
        if not via:
            raise ValueError("a path is read on the box: give the admin login (--via)")
        r = run(via, ids_command(source), timeout=60)
        where = f"the owner of {source}"
    else:
        r = run(source, ids_command(source), timeout=60)
        where = source
    if r.returncode != 0:
        raise ValueError(f"could not read the ids from {where} (exit {r.returncode}): "
                         f"{(r.stderr or r.stdout).strip()[-200:]}")
    return parse_ids(r.stdout, where)


def probe_script(user: str, uid: int, gid: int) -> str:
    """Read-only, as the admin login. Root checks go through `sudo -n`, never a plain test. Whether the admin
    may run the root script is asked with `sudo -n -l bash` (the command `--run` uses), never `sudo -n true`:
    a host may grant passwordless sudo only for specific commands."""
    u = shlex.quote(user)
    return (
        f"u={u}; "
        'echo "admin_user=$(id -un)"; echo "admin_home=$HOME"; '
        'if sudo -n -l bash >/dev/null 2>&1; then echo sudo=yes; else echo sudo=no; fi; '
        'echo "account=$(getent passwd "$u" | cut -d: -f3,4,6,7)"; '
        f'echo "uid_owner=$(getent passwd {int(uid)} | cut -d: -f1)"; '
        f'echo "gid_owner=$(getent group {int(gid)} | cut -d: -f1)"; '
        'getent group docker >/dev/null && echo docker=yes || echo docker=no; '
        'base=$(useradd -D 2>/dev/null | sed -n "s/^HOME=//p"); echo "home_base=${base:-/home}"; '
        'h="${base:-/home}/$u"; '
        'if sudo -n -l bash >/dev/null 2>&1; then '
        'sudo -n test -e "$h" && echo "home_exists=yes" || echo "home_exists=no"; '
        'echo "home_owner=$(sudo -n stat -c %u "$h" 2>/dev/null)"; '
        'else echo home_exists=unknown; fi; true'
    )


def read_probe(out: str) -> dict:
    kv = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)
    acc = kv.get("account", "").strip()
    account = None
    if acc:
        a_uid, a_gid, home, shell = (acc.split(":") + ["", "", "", ""])[:4]
        account = {"uid": int(a_uid) if a_uid.isdigit() else None, "gid": int(a_gid) if a_gid.isdigit() else None,
                   "home": home, "shell": shell}
    return {"admin_user": kv.get("admin_user", "").strip(), "admin_home": kv.get("admin_home", "").strip(),
            "sudo": kv.get("sudo", "").strip() == "yes", "account": account,
            "uid_owner": kv.get("uid_owner", "").strip(), "gid_owner": kv.get("gid_owner", "").strip(),
            "docker": kv.get("docker", "").strip() == "yes",
            "home_base": kv.get("home_base", "/home").strip() or "/home",
            "home_exists": kv.get("home_exists", "unknown").strip(), "home_owner": kv.get("home_owner", "").strip()}


def decide(user: str, ids: dict, probe: dict) -> "tuple[List[str], List[str]]":
    """(what will be done, problems that stop it). Nothing here changes another account."""
    todo: List[str] = []
    problems: List[str] = []
    acc = probe["account"]
    uid, gid = ids["uid"], ids["gid"]
    if acc:
        if acc["uid"] != uid or acc["gid"] != gid:
            problems.append(f"{user} exists with uid {acc['uid']} gid {acc['gid']}, not {uid} {gid} as on "
                            f"{ids['from']}: it is never changed; ask the user (and the box's admin)")
        else:
            todo.append(f"{user} exists with uid {uid} gid {gid}: complete it (keys, groups, linger)")
    else:
        if probe["uid_owner"] and probe["uid_owner"] != user:
            problems.append(f"uid {uid} belongs to {probe['uid_owner']} on the box: never reused or changed; "
                            "ask the user")
        if probe["home_exists"] == "yes" and probe["home_owner"] not in ("", str(uid)):
            problems.append(f"{probe['home_base']}/{user} exists, owned by uid {probe['home_owner']}: "
                            "left alone; ask the user")
        if not problems:
            grp_note = (f"group {probe['gid_owner']} (gid {gid}) exists" if probe["gid_owner"]
                        else f"create group {ids.get('group') or user} with gid {gid}")
            todo.append(f"create {user} with uid {uid} gid {gid}, home {probe['home_base']}/{user}, "
                        f"shell {DEFAULT_SHELL}; {grp_note}")
    if not probe["sudo"]:
        problems.append("the admin login has no passwordless sudo for a root shell (`sudo -n -l bash`): the box's admin "
                        "creates the account; "
                        "setup-box does not")
    elif not problems:
        todo.append(f"sudo for {user}: the admin login has it, so a sudoers.d drop-in, checked with visudo -cf")
    if probe["docker"] and not problems:
        todo.append(f"add {user} to the docker group")
    if not problems:
        todo.append(f"authorized_keys for {user}; linger on, so its project daemons run as systemd user services")
    return todo, problems


def valid_key(line: str) -> bool:
    return bool(KEY_RE.match(line.strip())) and "\n" not in line.strip()


def read_keys(paths: List[str]) -> List[str]:
    """Public key lines from .pub files; `here` stands for every ~/.ssh/*.pub on this machine."""
    files: List[str] = []
    for p in paths or ["here"]:
        if p == "here":
            files += sorted(glob.glob(os.path.expanduser("~/.ssh/*.pub")))
        else:
            files.append(os.path.expanduser(p))
    keys: List[str] = []
    for f in files:
        if not f.endswith(".pub"):
            raise ValueError(f"{f}: only public keys (.pub) go to a box")
        for ln in pathlib.Path(f).read_text(encoding="utf-8").splitlines():
            if ln.strip() and not ln.startswith("#"):
                if not valid_key(ln):
                    raise ValueError(f"{f}: not a public key line")
                if ln.strip() not in keys:
                    keys.append(ln.strip())
    if not keys:
        raise ValueError("no public key found: give --pubkey <file.pub>")
    return keys


def _keys_block(keys: List[str], ak: str) -> str:
    return "".join(f"grep -qxF {shlex.quote(k)} {ak} || printf '%s\\n' {shlex.quote(k)} >> {ak}; " for k in keys)


def keys_script(keys: List[str]) -> str:
    """As the user, in their own home: add each key once, modes 700/600."""
    return ('set -eu; install -d -m 700 ~/.ssh; touch ~/.ssh/authorized_keys; '
            + _keys_block(keys, "~/.ssh/authorized_keys") + "chmod 600 ~/.ssh/authorized_keys; echo keys=ok")


def create_script(user: str, uid: int, gid: int, group: str, keys: List[str], *, sudo: bool,
                  shell: str = DEFAULT_SHELL, sudoers_dir: str = SUDOERS_DIR) -> str:
    """Run as root. It re-checks what `decide` saw, touches only `user` and refuses on any conflict."""
    if not valid_login(user):
        raise ValueError(f"{user!r} is not a valid login name")
    if group and not valid_login(group):
        raise ValueError(f"{group!r} is not a valid group name")
    u, sd = shlex.quote(user), shlex.quote(sudoers_dir)
    g = shlex.quote(group or user)
    lines = [
        "set -eu",
        f"u={u}; uid={int(uid)}; gid={int(gid)}; sd={sd}",
        'die() { echo "refused: $*" >&2; exit 3; }',
        'if acc=$(getent passwd "$u"); then',
        '  [ "$(echo "$acc" | cut -d: -f3,4)" = "$uid:$gid" ] || die "$u exists with other ids; never changed"',
        'else',
        '  o=$(getent passwd "$uid" | cut -d: -f1 || true); [ -z "$o" ] || die "uid $uid belongs to $o"',
        f'  getent group "$gid" >/dev/null || groupadd -g "$gid" {g}',
        '  base=$(useradd -D | sed -n "s/^HOME=//p"); h="${base:-/home}/$u"',
        '  if [ -e "$h" ]; then',
        '    [ "$(stat -c %u "$h")" = "$uid" ] || die "$h exists, owned by another uid"',
        f'    useradd -M -d "$h" -u "$uid" -g "$gid" -s {shlex.quote(shell)} "$u"',
        '  else',
        f'    useradd -m -u "$uid" -g "$gid" -s {shlex.quote(shell)} "$u"',
        '  fi',
        'fi',
        'h=$(getent passwd "$u" | cut -d: -f6)',
    ]
    if sudo:
        lines += [
            # A name with a dot: sudo skips it while it is checked; it goes in place only once visudo passed.
            'f="$sd/90-ttp-$u"',
            'if [ ! -e "$f" ]; then',
            '  tmp=$(mktemp "$sd/.ttp-$u.XXXXXX")',
            "  printf '%s ALL=(ALL) NOPASSWD:ALL\\n' \"$u\" > \"$tmp\"; chmod 0440 \"$tmp\"",
            '  if ! visudo -cf "$tmp" >/dev/null; then rm -f -- "$tmp"; die "sudoers drop-in failed visudo -cf; not installed"; fi',
            '  mv -f -- "$tmp" "$f"',
            'fi',
        ]
    lines += [
        'if getent group docker >/dev/null; then usermod -aG docker "$u"; fi',
        'install -d -m 700 -o "$uid" -g "$gid" "$h/.ssh"',
        'ak="$h/.ssh/authorized_keys"; touch "$ak"',
        _keys_block(keys, '"$ak"') + 'chown "$uid:$gid" "$ak"; chmod 600 "$ak"',
        'loginctl enable-linger "$u"',
        'echo "ok: $(id "$u")"',
    ]
    return "\n".join(lines) + "\n"


def migrate_moves(admin_home: str, user_home: str, paths: List[str]) -> List[dict]:
    """Each path under the admin's home to the same place under the user's home; ValueError otherwise."""
    ah, uh = admin_home.rstrip("/"), user_home.rstrip("/")
    moves = []
    for p in paths:
        p = os.path.normpath(p)
        if not p.startswith(ah + "/") or ".." in p.split("/"):
            raise ValueError(f"{p}: only paths inside the admin's home ({ah}) are moved")
        moves.append({"src": p, "dst": uh + p[len(ah):]})
    return moves


def migrate_script(user: str, gid: int, moves: List[dict]) -> str:
    """Run as root: `mv` then `chown -R`; a source that is gone is skipped, a target that exists stops it.
    It never deletes; it prints `rebuild=<dir>` for each venv and build tree that embeds the old path."""
    out = ["set -eu", f"u={shlex.quote(user)}; gid={int(gid)}"]
    for m in moves:
        s, d = shlex.quote(m["src"]), shlex.quote(m["dst"])
        out += [
            f'if [ ! -e {s} ]; then echo "gone={m["src"]}"; else',
            f'  [ ! -e {d} ] || {{ echo "refused: {m["dst"]} exists" >&2; exit 3; }}',
            f'  install -d -o "$u" -g "$gid" "$(dirname {d})"',
            f'  mv -- {s} {d}; chown -R "$u:$gid" {d}; echo "moved={m["dst"]}"',
            f'  find {d} -maxdepth 4 \\( -name pyvenv.cfg -o -name CMakeCache.txt \\) -printf "rebuild=%h\\n"',
            'fi',
        ]
    return "\n".join(out) + "\n"


def migrate_status_script(moves: List[dict]) -> str:
    """Read-only, as root (`sudo -n test`): where each move stands."""
    out = []
    for m in moves:
        s, d = shlex.quote(m["src"]), shlex.quote(m["dst"])
        out.append(f'if sudo -n test -e {s}; then if sudo -n test -e {d}; then echo "blocked={m["dst"]}"; '
                   f'else echo "pending={m["src"]}"; fi; elif sudo -n test -e {d}; then echo "moved={m["dst"]}"; '
                   f'else echo "missing={m["src"]}"; fi')
    return "; ".join(out) + "; true"


def read_migrate_status(out: str) -> List[str]:
    words = {"pending": "to move", "moved": "moved", "blocked": "target exists, refused", "missing": "missing"}
    return [f"{words.get(k, k)}: {v}" for k, v in (ln.split("=", 1) for ln in out.splitlines() if "=" in ln)]


def entry_fields(entry: dict, user: str, home: str, admin: Optional[str], workdir: Optional[str] = None) -> dict:
    """The site entry with the login user, the admin alias (when needed) and a workdir_root in the home."""
    if not valid_login(user):
        raise ValueError(f"{user!r} is not a valid login name")
    home = home.rstrip("/")
    e = dict(entry)
    cur = workdir or e.get("workdir_root")
    if not cur or not under_home(cur, user, home):
        if workdir:
            raise ValueError(f"--workdir {workdir} is not under {user}'s home {home}")
        cur = f"{home}/work"
    e.update({"user": user, "workdir_root": cur})
    if admin:
        e["admin"] = admin
    else:
        e.pop("admin", None)
    return e


def under_home(path: str, user: str, home: Optional[str] = None) -> bool:
    p = path.rstrip("/") + "/"
    if p.startswith(("~/", "$HOME/")):
        return True
    if home:
        return p.startswith(home.rstrip("/") + "/")
    return f"/{user}/" in p


def check_account(alias: str, e: dict) -> List[str]:
    err = []
    if "user" in e:
        if not valid_login(e["user"]):
            err.append(f"{alias}: user must be a login name")
        elif not under_home(str(e.get("workdir_root", "")), e["user"]):
            err.append(f"{alias}: workdir_root must be under {e['user']}'s home (account.md)")
    if "admin" in e and (not box._str(e["admin"]) or e["admin"] == alias or any(c.isspace() for c in e["admin"])):
        err.append(f"{alias}: admin must be the admin login's own ssh alias, not the box's")
    return err


CHECKS = {route: check_account for route in box.ROUTES}


def _ssh_fail(alias: str, r) -> int:
    tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
    print(f"ssh: {alias} failed (exit {r.returncode}) {tail[0]}".rstrip())
    return 255 if r.returncode == 255 else 1


def _probe(args, ids) -> "tuple[Optional[dict], int]":
    r = box.ssh_run(args.via, probe_script(args.user, ids["uid"], ids["gid"]), timeout=60)
    if r.returncode != 0:
        return None, _ssh_fail(args.via, r)
    return read_probe(r.stdout), 0


def cmd_ids(args) -> int:
    try:
        ids = discover_ids(args.source, args.via)
    except ValueError as e:
        print(f"error: {e}")
        return 1
    print(json.dumps(ids))
    return 0


def cmd_probe(args) -> int:
    try:
        ids = discover_ids(args.ids_from, args.via)
    except ValueError as e:
        print(f"error: {e}")
        return 1
    probe, rc = _probe(args, ids)
    if probe is None:
        return rc
    todo, problems = decide(args.user, ids, probe)
    print(json.dumps({"ids": ids, "probe": probe, "todo": todo, "problems": problems}, indent=2))
    return 1 if problems else 0


def cmd_create(args) -> int:
    try:
        ids = discover_ids(args.ids_from, args.via)
        keys = read_keys(args.pubkey)
    except (ValueError, OSError) as e:
        print(f"error: {e}")
        return 1
    probe, rc = _probe(args, ids)
    if probe is None:
        return rc
    todo, problems = decide(args.user, ids, probe)
    for t in todo:
        print(f"do: {t}")
    for p in problems:
        print(f"problem: {p}")
    if problems:
        return 1
    script = create_script(args.user, ids["uid"], ids["gid"], ids.get("group") or "", keys, sudo=probe["sudo"])
    if not args.run:
        print("dry run; the root script (`--run` runs it through the admin login with sudo -n):")
        print(script)
        return 0
    r = box.ssh_run(args.via, "sudo -n bash -c " + shlex.quote(script), timeout=180)
    print((r.stdout + r.stderr).strip())
    return 0 if r.returncode == 0 else (255 if r.returncode == 255 else 1)


def cmd_keys(args) -> int:
    try:
        script = keys_script(read_keys(args.pubkey))
    except (ValueError, OSError) as e:
        print(f"error: {e}")
        return 1
    if not args.run:
        print(script)
        return 0
    r = box.ssh_run(args.alias, script, timeout=60)
    print((r.stdout + r.stderr).strip())
    return 0 if r.returncode == 0 else (255 if r.returncode == 255 else 1)


def cmd_migrate(args) -> int:
    r = box.ssh_run(args.via, f"echo \"admin_home=$HOME\"; getent passwd {shlex.quote(args.user)} | cut -d: -f4,6",
                    timeout=60)
    if r.returncode != 0:
        return _ssh_fail(args.via, r)
    lines = r.stdout.strip().splitlines()
    admin_home = next((ln.split("=", 1)[1] for ln in lines if ln.startswith("admin_home=")), "")
    acc = next((ln for ln in lines if ":" in ln and not ln.startswith("admin_home=")), "")
    if not acc:
        print(f"error: {args.user} does not exist on the box yet: `box.py account create` first")
        return 1
    gid, user_home = acc.split(":", 1)
    try:
        moves = migrate_moves(admin_home, user_home, args.path)
    except ValueError as e:
        print(f"error: {e}")
        return 1
    script = migrate_script(args.user, int(gid), moves)
    if not args.run:
        st = box.ssh_run(args.via, migrate_status_script(moves), timeout=60)
        if st.returncode != 0:
            return _ssh_fail(args.via, st)
        for m, ln in zip(moves, read_migrate_status(st.stdout)):
            print(f"{ln} ({m['src']} -> {m['dst']})")
        print("dry run; the root script (`--run` runs it with sudo -n):")
        print(script)
        return 0
    r = box.ssh_run(args.via, "sudo -n bash -c " + shlex.quote(script), timeout=1800)
    print((r.stdout + r.stderr).strip())
    rebuild = sorted({ln.split("=", 1)[1] for ln in r.stdout.splitlines() if ln.startswith("rebuild=")})
    if rebuild:
        print("rebuild (a moved venv or build tree keeps the old path): " + ", ".join(rebuild))
    return 0 if r.returncode == 0 else (255 if r.returncode == 255 else 1)


def cmd_entry(args) -> int:
    from box_setup import save_site
    try:
        site = box.load_site(args.site)
        entry = box.box_entry(site, args.alias)
        home = args.home
        if not home:
            r = box.ssh_run(args.alias, 'echo "$HOME"', timeout=60)
            if r.returncode != 0:
                return _ssh_fail(args.alias, r)
            home = r.stdout.strip()
        new = entry_fields(entry, args.user, home, args.admin, args.workdir)
    except (ValueError, box.SiteError) as e:
        print(f"error: {e}")
        return 1
    if new.get("workdir_root") != entry.get("workdir_root"):
        print(f"workdir_root: {entry.get('workdir_root') or '(none)'} -> {new['workdir_root']}")
    entry.clear()
    entry.update(new)
    save_site(box.site_path(args.site), site)
    print(f"saved {args.alias}: user {args.user}" + (f", admin alias {args.admin}" if args.admin else ""))
    return 0


def register(sub) -> None:
    ac = sub.add_parser("account", help="the user's own account on a box (account.md)") \
        .add_subparsers(dest="account_cmd", required=True)
    i = ac.add_parser("ids", help="the user's uid/gid: here, from an ssh alias, or a path's owner on the box")
    i.add_argument("source", nargs="?", default="here", help="here, an ssh alias, or an absolute path on the box")
    i.add_argument("--via", help="the admin login's ssh alias (to read a path on the box)")
    i.set_defaults(func=cmd_ids)
    for name, fn, hlp in (("probe", cmd_probe, "read-only: account, home (as root), sudo, docker, uid/gid owners"),
                          ("create", cmd_create, "create or complete the account (dry run unless --run)")):
        p = ac.add_parser(name, help=hlp)
        p.add_argument("alias")
        p.add_argument("--via", required=True, help="the admin login's ssh alias")
        p.add_argument("--user", default=local_user(), help="the login to create (default: your name here)")
        p.add_argument("--ids-from", default="here", help="here, an ssh alias, or a path on the box you own")
        if name == "create":
            p.add_argument("--pubkey", action="append", help="a .pub file (default: every ~/.ssh/*.pub here)")
            p.add_argument("--run", action="store_true")
        p.set_defaults(func=fn)
    k = ac.add_parser("keys", help="add public keys to authorized_keys as the user (dry run unless --run)")
    k.add_argument("alias")
    k.add_argument("--pubkey", action="append", help="a .pub file (default: every ~/.ssh/*.pub here)")
    k.add_argument("--run", action="store_true")
    k.set_defaults(func=cmd_keys)
    m = ac.add_parser("migrate", help="move work from the admin account into the user's home (dry run unless --run)")
    m.add_argument("alias")
    m.add_argument("--via", required=True, help="the admin login's ssh alias")
    m.add_argument("--user", default=local_user())
    m.add_argument("--path", action="append", required=True, help="an absolute path in the admin's home")
    m.add_argument("--run", action="store_true")
    m.set_defaults(func=cmd_migrate)
    e = ac.add_parser("entry", help="write user, admin alias and a workdir_root in the home into the site entry")
    e.add_argument("alias")
    e.add_argument("--user", default=local_user())
    e.add_argument("--admin", help="the admin login's ssh alias, when one is still needed")
    e.add_argument("--home", help="the user's home on the box (default: asked over ssh as the user)")
    e.add_argument("--workdir", help="workdir_root (default: the current one if in the home, else <home>/work)")
    e.set_defaults(func=cmd_entry)
