"""Project lease: one live coordinator per project across hosts (failover step L1).

The lease is a record in a store that sits off every host running the project, written only by
compare-and-swap (`read() -> (record, version)`, `cas(version, record)`). Its `epoch` goes up by one
on every change of holder and never on a renewal: runs get it as TTP_LEASE_EPOCH, the fencing
token later steps check. Not to be confused with provider failover (failover.py).

Timing never compares wall clocks across hosts. The holder counts itself the holder only until
`monotonic(start of its last successful renewal) + 0.8 * ttl`, renewing every ttl/5. Another host
counts a lease free only after it saw the same version unchanged for 1.25 * ttl on its own
monotonic clock. After a sleep the lease counts as lapsed until a renewal succeeds.

Backends: `git` (the ref refs/ttp/lease/<identity> on `failover.lease_remote`, CAS by
`git push --force-with-lease`). With no `failover.lease_remote` there is no lease: the daemon
behaves as on a single host (always the holder). The store interface is the seam for the ssh-file
backend (L2) and the stand-in's forced lease (S1)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from . import gitmemo

DEFAULT_TTL_S = 300
HOLD_FRACTION = 0.8        # the holder stops counting itself the holder this far into a ttl
TAKE_FRACTION = 1.25       # another host takes a lease seen unchanged this long
RENEW_FRACTION = 0.2       # the holder renews this often
STATE_FILE = "lease.json"  # under state/: what this host last saw, for status and runs
REF_PREFIX = "refs/ttp/lease/"
GIT_TIMEOUT_S = 30
# Lease refs never go on the plugin's own published repository.
OWN_REPOS = ("github.com/tenstorrent/skills",)


class Unreachable(Exception):
    """The store did not answer: the holder lets its lease lapse, nobody takes it."""


class Rejected(Unreachable):
    """The store answered but refused the write (a hook or ruleset): an error, not a lost race."""


class Refused(ValueError):
    """The configured store may not hold lease refs."""


# Configuration -----------------------------------------------------------------------------------

def settings(cfg: dict) -> dict:
    f = cfg.get("failover") or {}
    return f if isinstance(f, dict) else {}


def configured(cfg: dict) -> bool:
    """A lease is in force only with a store configured: none is today's single-host behaviour."""
    return bool(str(settings(cfg).get("lease_remote") or "").strip())


def ttl_s(cfg: dict) -> float:
    try:
        v = float(settings(cfg).get("lease_ttl_s") or DEFAULT_TTL_S)
    except (TypeError, ValueError):
        v = DEFAULT_TTL_S
    return max(v, 30.0)


def identity(p, cfg: dict) -> str:
    """failover.fleet when set (projects that drive one fleet share one lease), else the project id."""
    raw = str(settings(cfg).get("fleet") or "").strip() or str(cfg.get("id") or "") or p.name
    return re.sub(r"[^A-Za-z0-9._-]", "-", raw).strip(".-") or "project"


def norm_url(url: str) -> str:
    """A remote URL reduced to host/path for comparison: no scheme, user, port, `.git` or slash."""
    u = url.strip()
    # scp form [user@]host:path (git's rule: no slash before the first colon); host may be an ssh alias
    if "://" not in u and (m := re.match(r"^(?:[^/:@]+@)?([^/:@]+):(.*)$", u)):
        u = f"{m.group(1)}/{m.group(2)}"
    u = re.sub(r"^[a-z+]+://", "", u, flags=re.I)
    u = re.sub(r"^[^/@]+@", "", u)
    u = re.sub(r"^([^/:]+):\d+/", r"\1/", u)
    if u.startswith("/") or u.startswith("."):
        u = str(Path(u).expanduser().resolve())
    u = re.sub(r"/{2,}", "/", u)
    return re.sub(r"(\.git)?/*$", "", u).lower()


def git_url(url: str, cwd: Path | str | None) -> str:
    """`url` (or a remote name) as git would use it, after its url.<base>.insteadOf rewriting."""
    try:
        r = _git(["ls-remote", "--get-url", url], cwd, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return url
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else url


def push_url(url: str, cwd: Path | str | None) -> str:
    """`url` after git's url.<base>.pushInsteadOf rewriting (longest base wins), which a push uses
    before insteadOf; `url` itself when none matches."""
    try:
        r = _git(["config", "--get-regexp", r"^url\..*\.pushinsteadof$"], cwd, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return url
    best = ("", "")
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        key, _, prefix = line.partition(" ")
        base = key[len("url."):-len(".pushinsteadof")]
        if prefix and url.startswith(prefix) and len(prefix) > len(best[0]):
            best = (prefix, base)
    return best[1] + url[len(best[0]):] if best[0] else url


def ssh_hostname(host: str) -> str:
    """The host an ssh alias stands for, as `ssh -G` reports it (no connection); `host` if unknown."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", host):
        return host
    try:
        r = subprocess.run(["ssh", "-G", host], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return host
    m = re.search(r"^hostname\s+(\S+)\s*$", r.stdout, re.M | re.I) if r.returncode == 0 else None
    return m.group(1).lower() if m else host


def canon_url(url: str) -> str:
    """norm_url, with the host of an ssh URL (scp form or ssh://) replaced by the one its ssh alias
    stands for, so `alias:org/repo` and `git@real.host:org/repo` compare equal."""
    n, u = norm_url(url), url.strip()
    ssh = re.match(r"^(?:git\+ssh|ssh\+git|ssh)://", u, re.I) or (
        "://" not in u and not u.startswith(("/", ".", "~")) and re.match(r"^(?:[^/:@]+@)?[^/:@]+:", u))
    host = n.split("/", 1)[0]
    if not ssh or not host:
        return n
    real = ssh_hostname(host)
    return real + n[len(host):] if real != host else n


def url_forms(url: str, cwd: Path | str | None, name: str | None = None) -> set[str]:
    """Every host/path a fetch or push of `url` (or the remote `name`) may reach: as written, after
    insteadOf and after pushInsteadOf (and the remote's own push URLs), each with ssh aliases resolved."""
    urls = {url, git_url(name or url, cwd), push_url(url, cwd)}
    if name:
        try:
            r = _git(["remote", "get-url", "--push", "--all", name], cwd, timeout=10)
            urls |= set(r.stdout.split()) if r.returncode == 0 else set()
        except (OSError, subprocess.SubprocessError):
            pass
    return {canon_url(u) for u in urls}


def _owner_repo(n: str) -> str:
    return "/".join(n.split("/")[-2:])


def _git(args: list[str], cwd: Path | str | None = None, *, input: str | None = None,
         timeout: float = GIT_TIMEOUT_S, env: dict | None = None) -> subprocess.CompletedProcess:
    e = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    e.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=10")
    return subprocess.run(["git", *args], cwd=cwd, input=input, capture_output=True, text=True,
                          timeout=gitmemo.timeout(timeout, args), env=e)


def own_repos() -> set[str]:
    """This plugin's repository (its manifest's `repository`) and the built-in list."""
    out = {norm_url(u) for u in OWN_REPOS}
    manifest = Path(__file__).resolve().parents[2] / ".claude-plugin" / "plugin.json"
    try:
        repo = json.loads(manifest.read_text()).get("repository")
        if isinstance(repo, str) and repo:
            out.add(norm_url(repo))
    except (OSError, ValueError, AttributeError):
        pass
    return out


def delivery_remotes(root: Path) -> dict[str, str]:
    """name -> URL of the project's code repository remotes (its delivery remotes)."""
    try:
        r = _git(["remote", "-v"], root, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    out: dict[str, str] = {}
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        parts = line.split()
        if len(parts) >= 2:
            out.setdefault(parts[0], parts[1])
    return out


def resolve_remote(p, cfg: dict) -> str:
    """failover.lease_remote as a URL (a remote name of the code repository is looked up), or raise
    Refused saying why it may not hold lease refs: the plugin's own repository never; the project's
    delivery remotes only with failover.allow_delivery_remote."""
    raw = str(settings(cfg).get("lease_remote") or "").strip()
    if not raw:
        raise Refused("failover.lease_remote is not set")
    remotes = delivery_remotes(p.root)
    url = remotes.get(raw, raw)
    cwd = p.root if Path(p.root).is_dir() else None
    forms = url_forms(url, cwd, raw if raw in remotes else None)
    own = {_owner_repo(o) for o in own_repos()}
    if any(_owner_repo(n) in own for n in forms):          # any host or alias: by its owner/repo path
        raise Refused(f"failover.lease_remote {raw} is the tt-project plugin's own repository: lease refs "
                      f"may not be pushed there; configure a separate remote")
    if not settings(cfg).get("allow_delivery_remote"):
        for name, other in remotes.items():
            if url_forms(other, cwd, name) & forms:
                raise Refused(f"failover.lease_remote {raw} is the project's delivery remote ({name}): refs "
                              f"under {REF_PREFIX} may break its branch rules; configure a separate remote, or "
                              f"set failover.allow_delivery_remote if its charter allows such refs")
    if raw not in remotes:
        return url
    # A named remote's lease lives where `git push <name>` writes: its push URL (remote.<name>.pushurl
    # or a pushInsteadOf rewrite), not the fetch URL `git remote -v` lists first. push_sides gives the
    # name back for a one-URL remote. Several push URLs are refused rather than read from the first as
    # push_side does: a compare-and-swap is atomic on one repository only, and a write that one URL
    # took and another refused would leave the copies apart for good.
    from .push import push_sides
    sides = push_sides(Path(p.root), raw)
    if len(sides) > 1:
        raise Refused(f"failover.lease_remote {raw} has {len(sides)} push URLs: a lease needs one "
                      f"compare-and-swap point; configure a remote with a single push URL")
    return remotes.get(sides[0], sides[0])


# Store -------------------------------------------------------------------------------------------

class GitStore:
    """The lease as the ref refs/ttp/lease/<identity> on a remote: each write is a fresh orphan
    commit holding lease.json, pushed with --force-with-lease=<ref>:<old oid> (empty: must not
    exist), which receive-pack checks under the ref lock, so a write is atomic. `version` is the
    commit id ("" while no record exists). Objects live in a private bare repo under state/."""

    def __init__(self, remote: str, ident: str, gitdir: Path):
        self.remote, self.ref, self.gitdir = remote, REF_PREFIX + ident, Path(gitdir)
        self._url: str | None = None

    def _repo(self) -> Path:
        if not (self.gitdir / "HEAD").exists():
            self.gitdir.mkdir(parents=True, exist_ok=True)
            r = _git(["init", "-q", "--bare", str(self.gitdir)])
            if r.returncode:
                raise Unreachable(f"cannot create {self.gitdir}: {r.stderr.strip()[:200]}")
        return self.gitdir

    def _call(self, args: list[str], **kw) -> subprocess.CompletedProcess:
        try:
            return _git(["--git-dir", str(self._repo()), *args], **kw)
        except subprocess.TimeoutExpired as e:
            raise Unreachable(f"git {args[0]} timed out after {e.timeout:.0f} s") from e
        except OSError as e:
            raise Unreachable(f"git {args[0]}: {e}") from e

    def url(self) -> str:
        """Where a write goes: `remote` after the pushInsteadOf rewriting `git push` applies to it in
        this repo. Reads and the fetch ask this URL too, so the compare-and-swap's expected value comes
        from the repository it writes to (`git ls-remote <remote>` would ask the insteadOf form)."""
        if self._url is None:
            self._url = push_url(self.remote, self._repo())
        return self._url

    def read(self) -> tuple[dict | None, str]:
        r = self._call(["ls-remote", self.url(), self.ref])
        if r.returncode:
            raise Unreachable(f"git ls-remote {self.remote}: {(r.stderr or r.stdout).strip()[-300:]}")
        oid = next((ln.split()[0] for ln in r.stdout.splitlines() if ln.endswith("\t" + self.ref)), "")
        if not oid:
            return None, ""
        if self._call(["cat-file", "-e", oid + "^{commit}"]).returncode:
            f = self._call(["fetch", "-q", "--no-tags", self.url(), f"+{self.ref}:refs/ttp/seen"])
            if f.returncode:
                raise Unreachable(f"git fetch {self.remote}: {f.stderr.strip()[-300:]}")
        blob = self._call(["cat-file", "blob", f"{oid}:lease.json"])
        try:
            rec = json.loads(blob.stdout) if blob.returncode == 0 else None
        except ValueError:
            rec = None
        return (rec if isinstance(rec, dict) else {"unreadable": True}), oid

    def cas(self, version: str, rec: dict) -> bool:
        """Write `rec` if the record is still at `version`. False: someone else wrote first."""
        blob = self._call(["hash-object", "-w", "--stdin"], input=json.dumps(rec, indent=1, sort_keys=True) + "\n")
        tree = self._call(["mktree"], input=f"100644 blob {blob.stdout.strip()}\tlease.json\n")
        ident = {"GIT_AUTHOR_NAME": "ttp lease", "GIT_AUTHOR_EMAIL": "ttp-lease@localhost",
                 "GIT_COMMITTER_NAME": "ttp lease", "GIT_COMMITTER_EMAIL": "ttp-lease@localhost"}
        commit = self._call(["commit-tree", tree.stdout.strip(), "-m",
                             f"lease epoch {rec.get('epoch')} {rec.get('holder_host') or 'released'}"], env=ident)
        if blob.returncode or tree.returncode or commit.returncode:
            raise Unreachable("cannot build the lease commit: " + (commit.stderr or tree.stderr or blob.stderr).strip()[:200])
        r = self._call(["push", "--porcelain", f"--force-with-lease={self.ref}:{version}", self.url(),
                        f"{commit.stdout.strip()}:{self.ref}"])
        if r.returncode == 0:
            return True
        out = r.stdout + r.stderr
        if re.search(r"^!.*\[rejected\] \((stale info|fetch first)\)", r.stdout, re.M):
            return False
        # Another writer held or moved the ref under receive-pack's lock: also a lost race.
        if re.search(r"^!.*\[remote rejected\] \((failed to update ref|cannot lock ref)", r.stdout, re.M):
            return False
        if bang := re.search(r"^!\s.*?(\[.*)$", r.stdout, re.M):    # the remote's reason first: status cuts it
            said = "; ".join(ln[7:].strip() for ln in r.stderr.splitlines() if ln.startswith("remote:") and ln[7:].strip())
            raise Rejected(f"{bang.group(1).strip()}{': ' + said if said else ''} (git push {self.remote})"[:300])
        raise Unreachable(f"git push {self.remote}: {out.strip()[-300:]}")


# Lease -------------------------------------------------------------------------------------------

class Lease:
    """This host's view of the lease. `step()` acquires, renews or watches, at most every ttl/5
    while held; `held()` is the per-call check. The clock is injectable for tests."""

    def __init__(self, store, ident: str, *, host: str, base: str, ttl: float = DEFAULT_TTL_S,
                 state_file: Path | None = None, clock=time.monotonic):
        self.store, self.identity, self.host, self.base, self.ttl = store, ident, host, str(base), float(ttl)
        self.instance = uuid.uuid4().hex[:12]
        self.state_file, self.clock = state_file, clock
        self.held_until = 0.0          # monotonic deadline of the holder's own claim
        self.renew_start = None        # monotonic start of the last successful renewal
        self.last_check = None         # monotonic start of the last store round
        self.rec: dict | None = None   # the record as last read or written
        self.version = ""
        self.seen = ("", 0.0)          # (version, monotonic time first seen) of another holder's record
        self.error = ""
        self.error_kind = ""           # 'unreachable' or 'rejected' (the store refused the write)
        self.max_epoch = int((self._saved() or {}).get("max_epoch") or 0)
        self.checked_wall = 0.0

    # views
    def held(self) -> bool:
        return self.clock() < self.held_until

    @property
    def epoch(self) -> int:
        return int((self.rec or {}).get("epoch") or 0)

    def ours(self, rec: dict | None) -> bool:
        """Held by this project on this host: an earlier daemon here is dead (state/daemon.lock is
        one per host), so its lease is renewed after a restart, not waited out."""
        return bool(rec) and rec.get("holder_host") == self.host and rec.get("holder_base") == self.base

    def lapse(self) -> None:
        """After a sleep the monotonic deadline means nothing: held again only once a renewal succeeds.
        A watched record's age restarts too."""
        self.held_until, self.renew_start, self.last_check = 0.0, None, None
        self.seen = (self.seen[0], self.clock())

    def _record(self, base: dict | None, epoch: int, now_wall: float) -> dict:
        rec = dict(base or {})
        rec.pop("unreadable", None)
        rec.pop("released", None)
        rec.update({"identity": self.identity, "epoch": epoch, "holder_host": self.host,
                    "holder_base": self.base, "holder_instance": self.instance,
                    "role": rec.get("role") if self.ours(base) else "primary",
                    "primary_host": rec.get("primary_host") if self.ours(base) else self.host,
                    "ttl_s": self.ttl, "renewed_wall": now_wall})
        rec["role"] = rec["role"] or "primary"
        if not self.ours(base):
            rec["transitions"] = int(rec.get("transitions") or 0) + 1
        return rec

    def step(self, force: bool = False) -> str:
        """One acquire/renew/watch round. Returns what happened: 'kept' (no store call needed),
        'renewed', 'resumed' (this host's record from before a restart), 'acquired' (free), 'taken' (after
        another holder's record stood still 1.25 ttl), 'lost', 'watching' (another holder; also between
        rounds) or 'unreachable'. Watching reads the store at the same ttl/5 pace as renewing."""
        start = self.clock()
        if not force and self.last_check is not None and start - self.last_check < self.ttl * RENEW_FRACTION:
            return "kept" if self.held() else "watching"
        self.last_check = start
        now_wall = time.time()
        self.checked_wall = now_wall
        try:
            rec, ver = self.store.read()
            if rec:
                self.max_epoch = max(self.max_epoch, int(rec.get("epoch") or 0))
            if rec is None or rec.get("released") or self.ours(rec):
                fresh = rec is None or rec.get("released")
                new = self._record(rec, self.max_epoch + 1 if fresh else int(rec.get("epoch") or 0), now_wall)
                what = "acquired" if fresh else ("renewed" if rec.get("holder_instance") == self.instance
                                                 else "resumed")
            else:
                if ver != self.seen[0]:
                    self.seen = (ver, start)
                self.rec, self.version = rec, ver
                if start - self.seen[1] < self.ttl * TAKE_FRACTION:
                    was = self.held()
                    self.held_until, self.renew_start, self.error = 0.0, None, ""
                    self._save()
                    return "lost" if was else "watching"
                new, what = self._record(rec, self.max_epoch + 1, now_wall), "taken"
            if not self.store.cas(ver, new):
                self.held_until, self.renew_start = 0.0, None
                self.error = ""
                self.seen = ("", start)
                self._save()
                return "lost"
        except Unreachable as e:
            self.error = str(e)[:300]
            self.error_kind = "rejected" if isinstance(e, Rejected) else "unreachable"
            self._save()
            return self.error_kind
        self.rec, self.version, self.error, self.error_kind = new, "", "", ""
        self.max_epoch = max(self.max_epoch, new["epoch"])
        self.renew_start, self.held_until = start, start + self.ttl * HOLD_FRACTION
        self._save()
        return what

    def release(self) -> bool:
        """Give the lease up (planned stop or move): the record stays, marked released at its epoch, so
        the next holder takes it at once with epoch + 1. False when this host does not hold it."""
        try:
            rec, ver = self.store.read()
            if not self.ours(rec) or rec.get("released"):
                return False
            ok = self.store.cas(ver, {**rec, "released": True, "renewed_wall": time.time()})
        except Unreachable as e:
            self.error = str(e)[:300]
            return False
        self.held_until, self.renew_start = 0.0, None
        self._save()
        return ok

    # state/lease.json
    def _saved(self) -> dict | None:
        try:
            return json.loads(self.state_file.read_text()) if self.state_file else None
        except (OSError, ValueError):
            return None

    def snapshot(self) -> dict:
        r = self.rec or {}
        return {"identity": self.identity, "host": self.host, "held": self.held(), "epoch": self.epoch,
                "max_epoch": self.max_epoch, "holder_host": r.get("holder_host"), "role": r.get("role"),
                "released": bool(r.get("released")), "renewed_wall": r.get("renewed_wall"),
                "ttl_s": self.ttl, "checked_wall": self.checked_wall, "error": self.error,
                "error_kind": self.error_kind if self.error else ""}

    def _save(self) -> None:
        if not self.state_file:
            return
        from .project import durable_write
        try:
            durable_write(self.state_file, json.dumps(self.snapshot(), indent=1))
        except OSError:
            pass


def save_refused(p, cfg: dict, why: str) -> None:
    """state/lease.json for a store the project may not use, so `ttp status` says so."""
    from .project import durable_write
    rec = {"identity": identity(p, cfg), "held": False, "ttl_s": ttl_s(cfg), "checked_wall": time.time(),
           "error": str(why)[:300], "error_kind": "refused"}
    try:
        durable_write(p.state / STATE_FILE, json.dumps(rec, indent=1))
    except OSError:
        pass


def for_project(p, cfg: dict, *, clock=time.monotonic, host: str | None = None) -> Lease:
    """The project's lease on its configured store (raises Refused for a store it may not use)."""
    from .project import hostname
    ident = identity(p, cfg)
    store = GitStore(resolve_remote(p, cfg), ident, p.state / "lease.git")
    return Lease(store, ident, host=host or hostname(), base=str(p.base), ttl=ttl_s(cfg),
                 state_file=p.state / STATE_FILE, clock=clock)


# Status ------------------------------------------------------------------------------------------

def _ago(s: float) -> str:
    s = max(0, int(s))
    return f"{s} s" if s < 120 else f"{s // 60} min" if s < 7200 else f"{s / 3600:.1f} h"


def status_line(p, cfg: dict, now: float | None = None) -> str | None:
    """One `ttp status` line from state/lease.json, or None with no lease configured."""
    if not configured(cfg):
        return None
    now = time.time() if now is None else now
    try:
        s = json.loads((p.state / STATE_FILE).read_text())
    except (OSError, ValueError):
        return "lease: not checked yet (the daemon takes it before any dispatch)"
    if s.get("error_kind") == "refused":
        return f"lease: refused ({s.get('error', '')[:200]}): no dispatch"
    stale = now - float(s.get("checked_wall") or 0) > float(s.get("ttl_s") or DEFAULT_TTL_S)
    tail = f"; last checked {_ago(now - float(s.get('checked_wall') or 0))} ago" if stale else ""
    renewed = f", renewed {_ago(now - float(s['renewed_wall']))} ago" if s.get("renewed_wall") else ""
    if s.get("error") and s.get("error_kind") == "rejected":
        line = f"lease: store rejected the lease write ({s['error'][:160]})" + \
               ("; held until it lapses" if s.get("held") else ": no dispatch")
    elif s.get("error"):
        line = f"lease: store unreachable ({s['error'][:160]})" + \
               ("; held until it lapses" if s.get("held") else ": no dispatch")
    elif s.get("held"):
        line = f"lease: held here (epoch {s.get('epoch')}{renewed})"
    elif s.get("holder_host") and not s.get("released"):
        line = (f"lease: NOT held here (held by {s['holder_host']}, {s.get('role') or 'primary'}, "
                f"epoch {s.get('epoch')}{renewed}): no dispatch")
    else:
        line = "lease: NOT held here (free): no dispatch until it is taken"
    return line + tail


def check(p, cfg: dict) -> tuple[bool, str]:
    """`ttp lease check`: is the store allowed, does it answer, and how long a read takes."""
    if not configured(cfg):
        return True, "no lease configured (failover.lease_remote unset): single-host, always the holder"
    try:
        url = resolve_remote(p, cfg)
    except Refused as e:
        return False, f"refused: {e}"
    store = GitStore(url, identity(p, cfg), p.state / "lease.git")
    t0 = time.monotonic()
    try:
        rec, ver = store.read()
    except Unreachable as e:
        return False, f"unreachable: {e}"
    ms = (time.monotonic() - t0) * 1000
    who = (f"held by {rec.get('holder_host')} at epoch {rec.get('epoch')}" + (" (released)" if rec.get("released") else "")
           if rec else "no lease record yet")
    return True, f"ok: {REF_PREFIX}{identity(p, cfg)} on {url}: {who}; read took {ms:.0f} ms"
