"""setup-box IRD route: `box.py ird list|status|reserve|extend|release|reboot` (see ird.md).

Every call is `ssh -n -o BatchMode=yes <ird.mgmt> ird ...`: ird runs only on the management host.
Acts only on reservations whose user column is the caller. A claim file per reservation (project,
task, pid, heartbeat) lets `release --stale` free only this project's abandoned reservations.
State lives under $TTP_IRD_DIR (default ~/.tt-project/ird): claims/, the ssh include file and
known_hosts/, one host key file per reservation (trust on first use, deleted on release).
Exit codes: 0 ok, 1 refused or failed, 255 the management host is unreachable.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import math
import os
import pathlib
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

import box

UNREACHABLE = 255
BAD_STATES = ("drain", "down", "fail", "maint", "reboot", "unknown")
FREE_STATES = ("free", "idle", "mixed")
DEFAULT_TTL_S = 3600
DEFAULT_WAIT_S = 270  # under the ~5 min a worker may block one tool call
LIST_RETRY_S = (2, 5)  # waits before re-reading `ird list` after it failed mid-reserve
# A duration field of an `ird list` row, as Slurm prints time used and time left: [d-][h:]m:ss.
DURATION_RE = re.compile(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d\d)$")
SEPS = "-_.:/"
BEGIN = "# >>> tt-project ird {id}"
END = "# <<< tt-project ird {id}"


class IrdError(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


def state_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("TTP_IRD_DIR") or "~/.tt-project/ird").expanduser()


def ssh_include() -> pathlib.Path:
    return state_dir() / "ssh-hosts"


def known_hosts(alias: str, rid: str) -> pathlib.Path:
    """The reservation's own known_hosts file. Each reservation is a fresh container whose host key
    is new, so a new reservation starts with an empty file and its key is accepted once
    (`accept-new`); a key that changes within the reservation still fails."""
    return state_dir() / "known_hosts" / f"{alias}.{rid}"


# --- reachability -------------------------------------------------------------------------------

def _ssh_g(alias: str) -> Dict[str, str]:
    try:
        r = subprocess.run([*box.ssh_cmd(), "-G", alias], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    cfg: Dict[str, str] = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition(" ")
        cfg.setdefault(k.lower(), v.strip())
    return cfg


def probe(alias: str, depth: int = 0) -> None:
    """Fail fast (IrdError 255) when `alias`'s first ssh hop has no DNS name or no route."""
    cfg = _ssh_g(alias)
    if not cfg or cfg.get("proxycommand", "none") != "none" or depth > 3:
        return  # ssh decides; a proxy command cannot be probed from here
    jump = cfg.get("proxyjump", "none")
    if jump != "none":
        hop = jump.split(",")[0].rsplit("@", 1)[-1]
        name, _, port = hop.partition(":")
        if port or "." in name:
            return _probe_tcp(alias, name, int(port or 22))
        return probe(name, depth + 1)
    _probe_tcp(alias, cfg.get("hostname") or alias, int(cfg.get("port") or 22))


def _probe_tcp(alias: str, host: str, port: int) -> None:
    hint = ("IRD is reachable only from networks that can reach the IRD service "
            "(e.g. the corporate VPN); run this from such a host")
    try:
        addrs = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise IrdError(f"unreachable: {alias}: no DNS name for its ssh host. {hint}", UNREACHABLE)
    for fam, typ, proto, _, addr in addrs:
        try:
            with socket.socket(fam, typ, proto) as s:
                s.settimeout(5)
                s.connect(addr)
                return
        except OSError:
            continue
    raise IrdError(f"unreachable: {alias}: no route to its ssh port {port}. {hint}", UNREACHABLE)


def parse_list(text: str) -> List[dict]:
    """Rows of `ird list --noheader` output: id, user, machine, port and the raw fields."""
    rows = []
    for line in text.splitlines():
        f = line.split()
        if len(f) < 5 or not f[0].isdigit():
            continue  # header or log lines
        port = f[10] if len(f) > 10 and f[10].isdigit() else ""
        rows.append({"id": f[0], "user": f[1], "machine": f[4], "port": port, "fields": f})
    return rows


# --- ird over ssh -------------------------------------------------------------------------------

class Ctx:
    def __init__(self, args):
        try:
            self.entry = box.box_entry(box.load_site(args.site), args.alias)
        except box.SiteError as e:
            raise IrdError(str(e))
        if self.entry.get("route") != "ird" or not isinstance(self.entry.get("ird"), dict):
            raise IrdError(f"{args.alias} is not an ird box (route {self.entry.get('route')!r})")
        self.alias = args.alias
        self.cfg = self.entry["ird"]
        self.mgmt = self.cfg.get("mgmt", "")
        self.pools = self.cfg.get("pools") or []
        self.avoid = set(self.entry.get("excluded_nodes") or []) | set(self.entry.get("demo_nodes") or [])
        self.project = getattr(args, "project", None) or os.environ.get("TTP_PROJECT", "")
        self.task = getattr(args, "task", None) or os.environ.get("TTP_TASK", "")
        self._user = getattr(args, "user", None) or self.cfg.get("user")
        probe(self.mgmt)

    def run(self, *argv: str, timeout: float = 120) -> subprocess.CompletedProcess:
        r = box.ssh_run(self.mgmt, list(argv), timeout=timeout, opts=("-n",))
        if r.returncode == 255:
            raise IrdError(f"unreachable: ssh to {self.mgmt} failed: {r.stderr.strip()[-300:]}. "
                           "IRD is reachable only from networks that can reach the IRD service",
                           UNREACHABLE)
        return r

    def call(self, *argv: str, timeout: float = 120) -> subprocess.CompletedProcess:
        return self.run("ird", *argv, timeout=timeout)

    @property
    def user(self) -> str:
        if not self._user:
            r = self.run("id", "-un", timeout=30)
            self._user = r.stdout.strip()
            if r.returncode or not self._user:
                raise IrdError(f"cannot tell your user on {self.mgmt}; set ird.user in the site file")
        return self._user

    def reservations(self) -> List[dict]:
        """All rows of `ird list --noheader`: id, user, machine, port."""
        r = self.call("list", "--noheader")
        if r.returncode:
            raise IrdError(f"ird list failed (rc {r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")
        return parse_list(r.stdout)

    def mine(self) -> Dict[str, dict]:
        return {r["id"]: r for r in self.reservations() if r["user"] == self.user}

    def machines(self) -> List[dict]:
        r = self.call("list-machines")
        if r.returncode:
            raise IrdError(f"ird list-machines failed (rc {r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")
        out = []
        for line in r.stdout.splitlines():
            f = line.split()
            if len(f) < 5 or f[0].isupper():
                continue  # header or log lines
            out.append({"machine": f[0], "state": f[4], "why": classify(f[0], f[4], line, self.avoid)})
        return out


def classify(machine: str, state: str, line: str, avoid=()) -> str:
    """'' when the machine can be reserved now, else why not."""
    s = state.lower().strip("*~#!%$@+-")
    if machine in avoid:
        return "excluded in the site file"
    if "reboot asap" in line.lower():
        return "reboot pending (Reboot ASAP)"
    if s.startswith(BAD_STATES):
        return f"state {state}"
    if not s.startswith(FREE_STATES):
        return f"busy ({state})"
    return ""


# --- claims and the ssh include -----------------------------------------------------------------

def claim_path(alias: str, rid: str) -> pathlib.Path:
    return state_dir() / "claims" / f"{alias}.{rid}.json"


def read_claim(alias: str, rid: str) -> Optional[dict]:
    try:
        c = json.loads(claim_path(alias, rid).read_text(encoding="utf-8"))
        return c if isinstance(c, dict) else {}
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}


def write_claim(alias: str, rid: str, claim: dict) -> None:
    p = claim_path(alias, rid)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(claim, indent=1) + "\n", encoding="utf-8")
    tmp.replace(p)


def owner_pid(arg: Optional[int]) -> int:
    if arg:
        return arg
    run_dir = os.environ.get("TTP_RUN_DIR")
    if run_dir:
        try:
            return int(pathlib.Path(run_dir, "runner.pid").read_text().split()[0])
        except (OSError, ValueError, IndexError):
            pass
    return os.getppid()


def claim_live(c: dict, ttl: float) -> bool:
    """Fresh heartbeat, or its owner pid still runs on this host."""
    try:
        if time.time() - float(c.get("heartbeat", 0)) <= ttl:
            return True
    except (TypeError, ValueError):
        pass
    if c.get("host") != socket.gethostname():
        return False
    try:
        os.kill(int(c.get("pid") or 0), 0)
        return int(c.get("pid") or 0) > 0
    except (OSError, ValueError):
        return False


def write_ssh_block(alias: str, rid: str, machine: str, port: str, mgmt: str) -> None:
    kh = known_hosts(alias, rid)
    kh.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(kh.parent, 0o700)
    kh.write_text("", encoding="utf-8")  # a new reservation trusts no key from an earlier one
    os.chmod(kh, 0o600)
    lines = [BEGIN.format(id=rid), f"Host {machine}", f"    HostName {machine}"]
    if port:
        lines.append(f"    Port {port}")
    # Reached only through the authenticated jump host. Never StrictHostKeyChecking no, never
    # /dev/null: the key is pinned for the reservation's life, and the file goes with it.
    path = str(kh).replace("%", "%%").replace('"', "")
    lines += [f"    ProxyJump {mgmt}", f'    UserKnownHostsFile "{path}"',
              "    StrictHostKeyChecking accept-new", END.format(id=rid)]
    _edit_include(rid, "\n".join(lines) + "\n")


def drop_ssh_block(alias: str, rid: str) -> None:
    _edit_include(rid, "")
    kh = known_hosts(alias, rid)
    for p in (kh, kh.with_name(kh.name + ".old")):
        p.unlink(missing_ok=True)


def prune_known_hosts(ctx: "Ctx", live: Optional[set] = None, since: Optional[float] = None) -> None:
    """Drop the known_hosts file and ssh block of every reservation of this box that is gone from
    `ird list` (a run that crashed before releasing). Asks ird only when such a file exists; a file
    newer than `since` (when the reservations were read) is left for the next prune."""
    since = time.time() if since is None else since
    files = [p for p in (state_dir() / "known_hosts").glob(f"{ctx.alias}.*")
             if p.name[len(ctx.alias) + 1:].isdigit()]  # not another box whose alias starts so
    if not files:
        return
    if live is None:
        live = set(ctx.mine())
    for p in files:
        rid = p.name[len(ctx.alias) + 1:]
        try:
            fresh = p.stat().st_mtime >= since
        except OSError:
            continue
        if rid not in live and not fresh:
            drop_ssh_block(ctx.alias, rid)


def _edit_include(rid: str, block: str) -> None:
    p = ssh_include()
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    pat = re.compile(re.escape(BEGIN.format(id=rid)) + r"\n.*?" + re.escape(END.format(id=rid)) + r"\n?", re.S)
    text = pat.sub("", text) + block
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def include_hint() -> str:
    cfg = pathlib.Path("~/.ssh/config").expanduser()
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if str(ssh_include()) in text or "tt-project/ird/ssh-hosts" in text:
        return ""
    return (f"note: ~/.ssh/config does not include {ssh_include()}; ask the user to add "
            f"`Include {ssh_include()}` at its top")


# --- subcommands --------------------------------------------------------------------------------

def _gate(ctx: Ctx, rid: str, ttl: float, verb: str) -> Tuple[dict, Optional[dict]]:
    """The caller's own reservation `rid` and its claim; IrdError when we may not touch it."""
    rows = {r["id"]: r for r in ctx.reservations()}
    row = rows.get(rid)
    if row is None:
        raise IrdError(f"no reservation {rid} (already released?)")
    if row["user"] != ctx.user:
        raise IrdError(f"refused: reservation {rid} belongs to {row['user']}, not you; never {verb} it")
    c = read_claim(ctx.alias, rid)
    if c is None:
        raise IrdError(f"refused: reservation {rid} has no claim, so no task of this project made it; "
                       f"{verb} it by hand if it is yours to end")
    if c.get("project") != ctx.project:
        raise IrdError(f"refused: reservation {rid} is claimed by project {c.get('project')!r}")
    if c.get("task") != ctx.task and claim_live(c, ttl):
        raise IrdError(f"refused: reservation {rid} has a live claim of task {c.get('task')!r}")
    return row, c


def cmd_list(ctx: Ctx, args) -> int:
    prune_known_hosts(ctx)
    for m in ctx.machines():
        if not m["why"]:
            print(f"free {m['machine']} {m['state']}")
        elif args.all:
            print(f"skip {m['machine']} {m['why']}")
    return 0


def cmd_status(ctx: Ctx, args) -> int:
    mine = ctx.mine()
    for rid, r in sorted(mine.items(), key=lambda kv: int(kv[0])):
        c = read_claim(ctx.alias, rid)
        if c is None:
            who = "no claim"  # listed only: no cleanup path touches it
        else:
            who = (f"claim project={c.get('project')!r} task={c.get('task')!r} "
                   f"{'live' if claim_live(c, args.ttl) else 'stale'} expires={c.get('expires', '?')}")
        print(f"{rid} {r['machine']} port={r['port'] or '?'} {who}")
    if not mine:
        print(f"no reservations of {ctx.user}")
    return 0


TIMEOUT_RE = re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)$")


@contextlib.contextmanager
def reserve_lock(wait: float):
    """Serialize reserves sharing this state dir: from the before-snapshot to the written claim."""
    p = state_dir() / "reserve.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise IrdError(f"another `box.py ird reserve` held {p} for {wait:g}s; try again later")
                time.sleep(1)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# Lines ird prints on stderr that say nothing about the reservation (home-directory ownership and
# CA-bundle rehash on the management host): reported as warnings, never as the reserve's outcome.
NOISE_RE = re.compile(r"^(chown: changing ownership of .*: Operation not permitted"
                      r"|rehash: warning: skipping .*)$")
COMPLETE_RE = re.compile(r"\bon\s+(\S+?):?\s+Reservation complete", re.I)


def same_machine(a: str, b: str) -> bool:
    """`a` and `b` name one machine: `ird list-machines` may print a site prefix that `ird list`
    does not (or a domain suffix), so compare case-insensitively up to such a prefix or suffix."""
    a, b = a.strip().lower(), b.strip().lower()
    if not a or not b:
        return False
    if a == b or a.split(".")[0] == b.split(".")[0]:
        return True
    short, long_ = sorted((a, b), key=len)
    return any(long_.endswith(sep + short) for sep in SEPS)


def ird_name(name: str, machines: List[str], listed: List[str]) -> Tuple[str, str]:
    """(`name` as `ird list` prints it, '') or ('', why not). `ird reserve --machine` takes that
    form and places the reservation anywhere when given a name it does not know, such as the
    site-prefixed one `ird list-machines` may print. The prefix is learned from the machines both
    lists name; when they name none in common, or disagree, the form cannot be told."""
    listed_set = set(listed)
    if name in listed_set:
        return name, ""
    prefixes = set()
    for m in machines:
        for l in listed_set:
            if m == l:
                prefixes.add("")
            elif m.endswith(l) and m[:-len(l)][-1:] in SEPS:
                prefixes.add(m[:-len(l)])
    if not prefixes:
        return "", f"cannot tell the `ird list` form of {name} (no machine in both ird lists)"
    if len(prefixes) > 1:
        return "", f"cannot tell the `ird list` form of {name} (prefixes {sorted(prefixes)} disagree)"
    p = prefixes.pop()
    return (name[len(p):] if p and name.startswith(p) else name), ""


def durations(fields: List[str]) -> List[int]:
    """Seconds of each duration field of an `ird list` row (time used, time left), in row order."""
    out = []
    for f in fields:
        m = DURATION_RE.match(f)
        if m:
            d, h, mi, s = (int(x or 0) for x in m.groups())
            out.append(((d * 24 + h) * 60 + mi) * 60 + s)
    return out


def parse_expiry(s) -> Optional[dt.datetime]:
    """An ISO time with its zone (as reserve and extend print it), else None."""
    try:
        t = dt.datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return t if t.tzinfo else None


class Interrupted(Exception):
    pass


@contextlib.contextmanager
def interruptible():
    """SIGTERM and SIGHUP raise Interrupted (as Ctrl-C raises KeyboardInterrupt), so the reserve
    can clean up whatever landed before it exits. The first signal starts that cleanup and later
    ones are ignored: a TERM to the caller's process group reaches this process twice when GNU
    timeout runs it (directly, then forwarded by timeout), and a repeat must not cut the cleanup off
    before it has claimed what landed."""
    def raise_(signum, _frame):
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(s, signal.SIG_IGN)
        raise Interrupted(signal.Signals(signum).name)
    old = {s: signal.signal(s, raise_) for s in (signal.SIGTERM, signal.SIGHUP)}
    old[signal.SIGINT] = signal.getsignal(signal.SIGINT)
    try:
        yield
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def cmd_reserve(ctx: Ctx, args) -> int:
    m = TIMEOUT_RE.match(args.timeout)
    if not m:
        raise IrdError("--timeout must be hh:mm:ss: reserve only for the run")
    pool = args.pool or (ctx.pools[0] if ctx.pools else "")
    if pool not in ctx.pools:
        raise IrdError(f"pool {pool!r} is not in ird.pools of the site file")
    if args.machine:
        machines = ctx.machines()
        found = [x for x in machines if same_machine(x["machine"], args.machine)]
        if not found:
            raise IrdError(f"no machine {args.machine} in ird list-machines")
        if found[0]["why"] and not found[0]["why"].startswith("busy"):
            raise IrdError(f"refused: {args.machine}: {found[0]['why']}")
        name, why = ird_name(args.machine, [x["machine"] for x in machines],
                             [r["machine"] for r in ctx.reservations()])
        if why:
            print(f"warning: {why}; reserving without --machine")
    with reserve_lock(args.wait), interruptible():
        return _reserve(ctx, args, m, pool, name if args.machine else "")


def _reserve(ctx: Ctx, args, m, pool: str, name: str) -> int:
    # Never trust ird's exit code alone: it can fail after the reservation completed, or be cut
    # off by a timeout or a signal. The diff of `ird list` against this snapshot decides.
    since = time.time()
    before = set(ctx.mine())
    prune_known_hosts(ctx, before, since)
    argv = ["reserve", "--cluster", pool, "--timeout", args.timeout]
    if name:
        argv += ["--machine", name]
    if args.ssh_port:
        argv += ["--ssh-port", str(args.ssh_port)]
    argv += ["--no-shell", args.arch]  # every flag before the arch positional
    try:
        try:
            r = ctx.call(*argv, timeout=args.wait)
        except IrdError as e:  # ssh dropped (255): the reservation may have landed all the same
            r = subprocess.CompletedProcess(argv, e.code, "", str(e))
        text = r.stdout + "\n" + r.stderr
        landed = [x.group(1) for x in COMPLETE_RE.finditer(text)]
        try:
            rid, row, others = _pick_new_retrying(ctx, args, before, landed[-1] if landed else "")
        except IrdError as e:
            raise IrdError(f"ird reserve exited {r.returncode} and checking ird list failed: {e}. A "
                           f"reservation may have landed: check `box.py ird status {ctx.alias}` and "
                           f"release what you do not use", UNREACHABLE) from None
        if rid and r.returncode != 124:
            expires = _claim(ctx, args, rid, row, pool, m)
    except (KeyboardInterrupt, Interrupted) as e:
        _abort(ctx, args, before, pool, m, f"interrupted ({e or 'SIGINT'})")
        raise IrdError(f"interrupted during ird reserve ({e or 'SIGINT'})", 130)
    warnings = [f"ird: {l.strip()}" for l in r.stderr.splitlines() if NOISE_RE.match(l.strip())]
    if r.returncode == 124:
        if rid:
            done = _abort(ctx, args, before, pool, m, f"ird reserve timed out after {args.wait:g}s")
            raise IrdError(f"ird reserve timed out after {args.wait:g}s; reservation {rid} on "
                           f"{row['machine']} landed anyway: {done}")
        raise IrdError(f"ird reserve timed out after {args.wait:g}s; the reservation may still land: "
                       f"check `box.py ird status {ctx.alias}` and release what you do not use")
    if not rid:
        if others:
            raise IrdError(_ambiguous(ctx, others))
        if r.returncode == UNREACHABLE:
            raise IrdError(f"{r.stderr.strip()[-300:]}; no new reservation in ird list yet, but it may "
                           f"still land: check `box.py ird status {ctx.alias}` and release what you do "
                           f"not use", UNREACHABLE)
        rest = "\n".join(l for l in text.splitlines() if l.strip() and not NOISE_RE.match(l.strip()))
        raise IrdError(f"ird reserve made no reservation (rc {r.returncode}): {rest.strip()[-400:]}")
    for w in warnings:
        print(f"warning: {w}")
    if r.returncode:
        print(f"warning: ird reserve exited {r.returncode}, but reservation {rid} is in ird list: claimed it")
    if name and not same_machine(row["machine"], args.machine):
        print(f"warning: asked for {args.machine}, ird reserved {row['machine']}: use {row['machine']}")
    elif args.machine and not same_machine(row["machine"], args.machine):
        print(f"note: ird reserved {row['machine']}: use {row['machine']}")  # reserved without --machine
    print(f"reserved {rid} machine={row['machine']} port={row['port'] or '?'} expires={expires}")
    hint = include_hint()
    if hint:
        print(hint)
    return 0


def _pick_new(ctx: Ctx, args, before: set, landed: str) -> Tuple[str, Optional[dict], Dict[str, dict]]:
    """(id, row, {}) of this reserve's new reservation, or ('', None, candidates) when none or
    several. New = in the caller's `ird list` now, not in the snapshot, with no claim yet. Another
    reserve of the same user (another host, or by hand) may land meanwhile: narrow by the port
    asked for, then the machine ird says it reserved, then the machine asked for; never guess."""
    new = {k: v for k, v in ctx.mine().items() if k not in before and _unclaimed_or_ours(ctx, k)}
    if args.ssh_port:
        new = {k: v for k, v in new.items() if v["port"] == str(args.ssh_port)}
    for name in (landed, args.machine):
        if len(new) > 1 and name:
            narrowed = {k: v for k, v in new.items() if same_machine(v["machine"], name)}
            new = narrowed or new
    if len(new) == 1:
        rid, row = next(iter(new.items()))
        return rid, row, {}
    return "", None, new


def _pick_new_retrying(ctx: Ctx, args, before: set, landed: str):
    """_pick_new, retried a few times when `ird list` fails (ssh may be down for a moment)."""
    for delay in LIST_RETRY_S:
        try:
            return _pick_new(ctx, args, before, landed)
        except IrdError as e:
            print(f"warning: checking ird list failed ({e}); retrying in {delay:g}s")
            time.sleep(delay)
    return _pick_new(ctx, args, before, landed)


def _unclaimed_or_ours(ctx: Ctx, rid: str) -> bool:
    """No claim yet, or the claim this very process wrote (a signal right after writing it)."""
    c = read_claim(ctx.alias, rid)
    return c is None or (c.get("host") == socket.gethostname() and c.get("reserver") == os.getpid())


def _ambiguous(ctx: Ctx, rows: Dict[str, dict]) -> str:
    listed = ", ".join(f"{k} on {v['machine']}" for k, v in sorted(rows.items(), key=lambda kv: int(kv[0])))
    return (f"refused: several new reservations of yours ({listed}) appeared at once, so which one is "
            f"this run's is unclear; no claim written and none released. Check `box.py ird status "
            f"{ctx.alias}`, retry with --machine, and release by hand what you do not use")


def _claim(ctx: Ctx, args, rid: str, row: dict, pool: str, m) -> str:
    """Record the claim (and its ssh block) before anything reports or uses the reservation."""
    h, mi, s = map(int, m.groups())
    now = dt.datetime.now().astimezone()
    expires = (now + dt.timedelta(hours=h, minutes=mi, seconds=s)).isoformat(timespec="minutes")
    write_claim(ctx.alias, rid, {
        "id": rid, "box": ctx.alias, "mgmt": ctx.mgmt, "user": ctx.user, "machine": row["machine"],
        "port": row["port"], "pool": pool, "arch": args.arch, "project": ctx.project,
        "task": ctx.task, "run_id": os.environ.get("TTP_RUN_ID", ""), "host": socket.gethostname(),
        "pid": owner_pid(args.pid), "reserved_at": now.isoformat(timespec="seconds"),
        "heartbeat": time.time(), "expires": expires, "reserver": os.getpid()})
    write_ssh_block(ctx.alias, rid, row["machine"], row["port"], ctx.mgmt)
    return expires


def _abort(ctx: Ctx, args, before: set, pool: str, m, why: str) -> str:
    """The reserve is being abandoned: claim what it made (so the release passes the claim gate
    and nothing unclaimed is touched), then release it. Signals wait until this is done."""
    old = {s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        rid, row, others = _pick_new(ctx, args, before, "")
        if not rid:
            if others:
                print(f"error: {_ambiguous(ctx, others)}")
                return "not released (unclear which is this run's)"
            print(f"{why}: no new reservation in ird list; if one lands later, "
                  f"`box.py ird status {ctx.alias}` shows it")
            return "none landed"
        _claim(ctx, args, rid, row, pool, m)
        print(f"{why}: reservation {rid} on {row['machine']} landed; releasing it")
        try:
            _release(ctx, rid)
            print(f"released {rid} {row['machine']}")
            return "released it"
        except IrdError as e:
            print(f"error: {e}; release it with `box.py ird release {ctx.alias} {rid}`")
            return f"NOT released; release it with `box.py ird release {ctx.alias} {rid}`"
    except IrdError as e:
        print(f"error: {why}, and checking ird list failed: {e}; check `box.py ird status {ctx.alias}`")
        return f"unknown; check `box.py ird status {ctx.alias}`"
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def cmd_extend(ctx: Ctx, args) -> int:
    row, c = _gate(ctx, args.id, args.ttl, "extend")
    if args.minutes < 1:
        raise IrdError("minutes must be at least 1")
    old = parse_expiry(c.get("expires"))
    if old is None:
        raise IrdError(f"cannot parse the expiry {c.get('expires')!r} in the claim of {args.id}; not extending")
    # `ird change-timeout <id> <m>` sets the time left to m minutes; it does not add m.
    left_s = (old - dt.datetime.now().astimezone()).total_seconds()
    left = max(0, math.ceil(left_s / 60))
    target = left + args.minutes
    r = ctx.call("change-timeout", args.id, str(target))
    if r.returncode:
        raise IrdError(f"ird change-timeout {args.id} {target} failed (rc {r.returncode}): "
                       f"{(r.stderr or r.stdout).strip()[-300:]}")
    after = {x["id"]: x for x in ctx.reservations()}.get(args.id)
    now = dt.datetime.now().astimezone()
    if after is None:
        raise IrdError(f"reservation {args.id} is gone from ird list after ird change-timeout")
    # The time-left field is the one that grew (time used grows only by the seconds in between);
    # when none grew, the one nearest the time left the claim expected.
    pairs = list(zip(durations(row["fields"]), durations(after["fields"])))
    if not pairs:
        raise IrdError(f"ird change-timeout {args.id} {target} exited 0, but the ird list row has no time "
                       f"left to read the new expiry from: {' '.join(after['fields'])}")
    grew = max(pairs, key=lambda p: p[1] - p[0])
    _, now_left = grew if grew[1] - grew[0] >= 60 else min(pairs, key=lambda p: abs(p[0] - left_s))
    new = (now + dt.timedelta(seconds=now_left)).replace(second=0, microsecond=0)  # minutes, as old
    c.update(task=ctx.task or c.get("task", ""), heartbeat=time.time(), expires=new.isoformat(timespec="minutes"))
    write_claim(ctx.alias, args.id, c)
    gained = (new - old).total_seconds() / 60
    if gained < args.minutes - 1:
        raise IrdError(f"ird change-timeout {args.id} {target} exited 0, but ird list shows it expiring "
                       f"{c['expires']} (was {old.isoformat(timespec='minutes')}, {gained:+.0f} min, asked "
                       f"+{args.minutes}): row before: {' '.join(row['fields'])}; after: {' '.join(after['fields'])}")
    if gained > args.minutes + 2:
        print(f"warning: ird added {gained:.0f} min, more than the {args.minutes} asked; release early when done")
    print(f"extended {args.id} by {args.minutes} min: expires={c['expires']} "
          f"(was {old.isoformat(timespec='minutes')}, {gained:+.0f} min per ird list)")
    return 0


def _release(ctx: Ctx, rid: str) -> None:
    r = ctx.call("release", rid)
    if r.returncode and rid in ctx.mine():
        raise IrdError(f"ird release {rid} failed (rc {r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")
    claim_path(ctx.alias, rid).unlink(missing_ok=True)
    drop_ssh_block(ctx.alias, rid)


def cmd_release(ctx: Ctx, args) -> int:
    if not args.stale:
        if not args.id:
            raise IrdError("give a reservation id, or --stale")
        _gate(ctx, args.id, args.ttl, "release")
        if args.dry_run:
            print(f"would release {args.id}")
        else:
            _release(ctx, args.id)
            print(f"released {args.id}")
        return 0
    # Port of reap.sh: only the caller's own reservations with a stale claim of this project.
    for rid, row in sorted(ctx.mine().items(), key=lambda kv: int(kv[0])):
        c = read_claim(ctx.alias, rid)
        if c is None:
            print(f"skip {rid} {row['machine']}: no claim (not made by this project)")
        elif c.get("project") != ctx.project:
            print(f"skip {rid} {row['machine']}: claimed by project {c.get('project')!r}")
        elif claim_live(c, args.ttl):
            print(f"keep {rid} {row['machine']}: live claim of task {c.get('task')!r}")
        elif args.dry_run:
            print(f"would release {rid} {row['machine']}: stale claim of task {c.get('task')!r}")
        else:
            _release(ctx, rid)
            print(f"released {rid} {row['machine']}: stale claim of task {c.get('task')!r}")
    # Claims whose reservation is gone (expired) are dropped, with their ssh blocks.
    if not args.dry_run:
        live_ids = set(ctx.mine())
        for p in (state_dir() / "claims").glob(f"{ctx.alias}.*.json"):
            rid = p.name[len(ctx.alias) + 1:-len(".json")]
            if not rid.isdigit():
                continue  # another box whose alias starts with "<alias>."
            c = read_claim(ctx.alias, rid) or {}
            if rid not in live_ids and c.get("project") == ctx.project:
                p.unlink(missing_ok=True)
                drop_ssh_block(ctx.alias, rid)
    return 0


def cmd_reboot(ctx: Ctx, args) -> int:
    row, c = _gate(ctx, args.id, args.ttl, "reboot")
    r = ctx.call("reboot", args.id)
    if r.returncode:
        raise IrdError(f"ird reboot failed (rc {r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")
    _release(ctx, args.id)  # the reboot is drain-gated: it fires only once the reservation ends
    again = ["box.py", "ird", "reserve", ctx.alias, "--arch", c.get("arch") or "<arch>",
             "--machine", row["machine"], "--timeout", "<hh:mm:ss>"]
    if row["port"]:
        again += ["--ssh-port", row["port"]]
    print(f"reboot scheduled and {args.id} released; once `box.py ird list {ctx.alias}` shows "
          f"{row['machine']} free again, re-reserve it: {shlex.join(again)}")
    return 0


def _wrap(fn):
    def run(args) -> int:
        try:
            return fn(Ctx(args), args)
        except IrdError as e:
            print(f"error: {e}")
            return e.code
    return run


def register(sub) -> None:
    ird = sub.add_parser("ird", help="IRD reservations (ird.md)").add_subparsers(dest="ird_cmd", required=True)

    def add(name, fn, help_):
        p = ird.add_parser(name, help=help_)
        p.add_argument("alias", help="the box's alias in the site file")
        p.add_argument("--user", help="your user on the management host (default ird.user, else id -un there)")
        p.add_argument("--project", help="claim owner project (default $TTP_PROJECT)")
        p.add_argument("--task", help="claim owner task (default $TTP_TASK)")
        p.add_argument("--ttl", type=float, default=DEFAULT_TTL_S,
                       help="a claim with an older heartbeat and a dead owner pid is stale (seconds)")
        p.set_defaults(func=_wrap(fn))
        return p

    p = add("list", cmd_list, "machines you can reserve now")
    p.add_argument("--all", action="store_true", help="also show skipped machines and why")
    add("status", cmd_status, "your reservations and their claims")
    p = add("reserve", cmd_reserve, "reserve a machine for this run")
    p.add_argument("--arch", required=True)
    p.add_argument("--timeout", required=True, help="hh:mm:ss, the run's length")
    p.add_argument("--pool", help="default: the first of ird.pools")
    p.add_argument("--machine")
    p.add_argument("--ssh-port", type=int)
    p.add_argument("--pid", type=int, help="owner pid (default the run's runner.pid)")
    p.add_argument("--wait", type=float, default=DEFAULT_WAIT_S, help="seconds to wait for ird reserve")
    p = add("extend", cmd_extend, "extend your reservation (on demand, never automatically)")
    p.add_argument("id")
    p.add_argument("minutes", type=int)
    p = add("release", cmd_release, "release your reservation, or --stale ones of this project")
    p.add_argument("id", nargs="?")
    p.add_argument("--stale", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p = add("reboot", cmd_reboot, "schedule a reboot of your reservation's machine and release it")
    p.add_argument("id")
