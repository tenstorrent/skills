"""State snapshots for a standby host (design: project lease and failover, section 2, step R1).

With `failover.standby` set (a machine alias reached over ssh, or an absolute directory such as a
mounted share), the daemon writes a consistent snapshot of the project's state there every
`failover.snapshot_every_s` (and soon after a task finishes), keeping the newest
`failover.snapshot_keep`. Unset, nothing happens: single-host projects have nothing to turn on.

A snapshot holds:
- project.db.gz: the database through the sqlite3 backup API on its own connection (consistent with
  writers active, WAL included), secrets dropped from kv, `PRAGMA integrity_check` passed, gzipped;
- harness.bundle: the harness repo (charter, memory, project.json) as a git bundle: a full one at
  least daily, otherwise what is new since that full one (`chain_from` names it, so a snapshot needs
  only itself and its full one; retention keeps the newest `snapshot_keep` and the full ones they
  name);
- branches.bundle: open tasks' branches, refs/ttp/push/* and branches still queued for
  delivery.backup_remote, without what any remote already has;
- runs/<task>/: result.json, prompt.md and run.json of the latest run of each open task;
- shared.json: this project's open machine-ledger changes and the pauses of resources it names.
Run folders, logs, worktrees, web.token and credentials stay behind.

It is shipped as a tar stream into `<base>/<identity>/<seq>.partial/`, every file checked against
the manifest's sha256 and synced, then renamed to `<seq>/`: a reader never sees a half-written
snapshot, and a crash leaves only a `.partial` the next receive removes. The same stdlib receiver
(RECEIVER) runs locally or over ssh, and also verifies. `restore` turns a verified snapshot into a
project folder; a stand-in (S1) or hand-back (H1) applies `state/standby-import/` from there.
"""
from __future__ import annotations

import contextlib
import fcntl
import gzip
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

from . import project
from . import gitmemo

STATE = "replica.json"         # in state/: the last attempt and the last shipped snapshot
LOCK = "replica.lock"          # in state/: one snapshot in flight per project
BASE = "~/.tt-project/standby"  # on a standby reached over ssh
EVERY_S = 900
KEEP = 3
FULL_EVERY_S = 86400           # a full harness bundle at least this often
MIN_GAP_S = 120                # a finished task brings the next snapshot forward, no closer than this
STALE_MIN_S = 3600             # a snapshot older than max(this, 3 x interval) is stale
TIMEOUT_S = 900
RUN_FILES = ("result.json", "prompt.md", "run.json")
RUN_FILE_MAX = 2 << 20
OPEN = ("queued", "running", "waiting", "blocked", "needs_review", "pushing")
# kv keys never copied: anything that looks like a secret
DENY_KV = ("%token%", "%secret%", "%password%", "%credential%", "%api_key%", "%cookie%")
OBS_SOURCE = "daemon:replica"
OBS_KEY = "replica-stale"


class SnapshotError(Exception):
    pass


# settings -----------------------------------------------------------------------------------------
def settings(cfg: dict) -> dict | None:
    """The snapshot settings, or None while `failover.standby` is unset (off)."""
    f = (cfg or {}).get("failover") or {}
    dest = str(f.get("standby") or "").strip()
    if not dest:
        return None
    every = max(60.0, float(f.get("snapshot_every_s") or EVERY_S))
    return {"dest": dest, "every_s": every, "keep": max(1, int(f.get("snapshot_keep") or KEEP)),
            "stale_s": max(STALE_MIN_S, 3 * every)}


def identity(p: project.Project, cfg: dict) -> str:
    """failover.fleet when set, else the project's id, else its name: one folder per identity."""
    v = str(((cfg or {}).get("failover") or {}).get("fleet") or cfg.get("id") or p.name)
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in v)[:100] or "project"


def _target(cfg: dict, dest: str) -> tuple[str | None, str]:
    """(ssh host or None, base directory there)."""
    if dest.startswith("/"):
        return None, dest
    from . import hostmap
    return hostmap.resolve(cfg, dest), BASE


def load_state(p: project.Project) -> dict:
    try:
        v = json.loads((p.state / STATE).read_text())
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _save_state(p: project.Project, st: dict) -> None:
    project.write_json(p.state / STATE, st, 0o600)




# The receiver: stdlib only, run as `python3 -c` here (an absolute destination) or on the standby.
# `verify <base> <identity>`: checks every snapshot there. `receive <base> <identity> <seq> <keep>
# <chain_from>`: reads a tar stream into <seq>.partial, checks it against its manifest (the last
# member, so a cut stream never passes), syncs it, renames it into place and applies retention.
# It writes each received file itself and syncs it, its folders and the parent before the rename.
RECEIVER = r'''
import fcntl,hashlib,json,os,shutil,sys,tarfile
mode,base,ident=sys.argv[1],os.path.expanduser(sys.argv[2]),sys.argv[3]
d=os.path.join(base,ident)
def done(o,rc=0):
    print(json.dumps(o));sys.stdout.flush();sys.exit(rc)
def at(s): return os.path.join(d,'%06d'%s)
def seqs():
    try: return sorted(int(n) for n in os.listdir(d) if n.isdigit())
    except OSError: return []
def man(p):
    with open(os.path.join(p,'manifest.json')) as f: return json.load(f)
def chain_from(s):
    try: return int(man(at(s)).get('chain_from',s))
    except (OSError,ValueError,TypeError): return s
def check(p,s):
    try: m=man(p)
    except (OSError,ValueError) as e: return 'no readable manifest (%s)'%e
    if m.get('seq')!=s: return 'its manifest names #%s'%m.get('seq')
    files=m.get('files') or {}
    have=set()
    for r,_,fs in os.walk(p):
        for n in fs: have.add(os.path.relpath(os.path.join(r,n),p))
    extra=have-set(files)-{'manifest.json'}
    if extra: return 'files not in its manifest: %s'%', '.join(sorted(extra)[:3])
    for rel,v in sorted(files.items()):
        h=hashlib.sha256();n=0
        try:
            with open(os.path.join(p,rel),'rb') as f:
                for b in iter(lambda:f.read(1<<20),b''): h.update(b);n+=len(b)
        except OSError: return 'missing %s'%rel
        if h.hexdigest()!=v.get('sha256') or n!=v.get('size'): return 'checksum mismatch: %s'%rel
    return ''
def fsync(p,isdir=False):
    fd=os.open(p,os.O_RDONLY|(os.O_DIRECTORY if isdir else 0))
    try: os.fsync(fd)
    finally: os.close(fd)
os.makedirs(d,mode=0o700,exist_ok=True)
lk=os.open(os.path.join(d,'.lock'),os.O_RDWR|os.O_CREAT,0o600)
fcntl.flock(lk,fcntl.LOCK_EX)
if mode=='verify':
    ok=set();out=[]
    for s in seqs():
        why=check(at(s),s)
        if not why:
            c=chain_from(s)
            if c!=s and c not in ok: why='its chain lacks #%s, the full snapshot it builds on'%c
        if not why: ok.add(s)
        out.append({'seq':s,'ok':not why,'why':why})
    done({'dir':d,'snapshots':out})
if mode!='receive': done({'error':'unknown mode %s'%mode},2)
seq,keep,cf=int(sys.argv[4]),int(sys.argv[5]),int(sys.argv[6])
for n in os.listdir(d):
    if n.endswith(('.partial','.del')): shutil.rmtree(os.path.join(d,n),ignore_errors=True)
have=seqs()
if have and seq<=have[-1]: done({'error':'snapshot #%s or a newer one is already there'%have[-1],'next':have[-1]+1},3)
if cf!=seq and cf not in have: done({'error':'its chain lacks #%s there'%cf,'need_full':True},4)
part=at(seq)+'.partial'
os.mkdir(part,0o700)
try:
    with tarfile.open(fileobj=sys.stdin.buffer,mode='r|') as t:
        for m in t:
            n=os.path.normpath(m.name)
            if not m.isfile() or os.path.isabs(n) or n.split(os.sep)[0]=='..': raise ValueError('refused member %r'%m.name)
            dst=os.path.join(part,n);os.makedirs(os.path.dirname(dst),exist_ok=True)
            with open(dst,'wb') as f:
                shutil.copyfileobj(t.extractfile(m),f);f.flush();os.fsync(f.fileno())
except (OSError,ValueError,tarfile.TarError) as e:
    shutil.rmtree(part,ignore_errors=True);done({'error':'receiving failed: %s'%e},5)
why=check(part,seq)
if why: shutil.rmtree(part,ignore_errors=True);done({'error':'the received snapshot is not complete: '+why},5)
for r,ds,_ in os.walk(part,topdown=False):
    for n in ds: fsync(os.path.join(r,n),True)
fsync(part,True)
os.rename(part,at(seq));fsync(d,True)
have=seqs();kept=set(have[-keep:]);kept|={chain_from(s) for s in kept}
gone=[s for s in have if s not in kept]
for s in gone: os.rename(at(s),at(s)+'.del');shutil.rmtree(at(s)+'.del',ignore_errors=True)
if gone: fsync(d,True)
done({'seq':seq,'kept':sorted(s for s in have if s in kept),'removed':gone})
'''


def _receiver_cmd(cfg: dict, dest: str, *args: Any) -> list[str]:
    host, base = _target(cfg, dest)
    argv = [str(a) for a in (args[0], base, *args[1:])]
    if host is None:
        return [sys.executable, "-I", "-c", RECEIVER, *argv]
    from . import hostmap
    return [*hostmap.SSH, "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4", host,
            "python3 -I -c " + " ".join(shlex.quote(a) for a in (RECEIVER, *argv))]


def _answer(out: bytes, err: bytes, rc: int) -> dict:
    lines = out.decode(errors="replace").strip().splitlines()
    try:
        v = json.loads(lines[-1]) if lines else None
    except ValueError:
        v = None
    if not isinstance(v, dict):
        tail = (err or out).decode(errors="replace").strip()[-300:]
        v = {"error": f"the receiver exited {rc}: {tail or 'no output'}"}
    v["rc"] = rc
    return v


# building -----------------------------------------------------------------------------------------
def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=gitmemo.timeout(TIMEOUT_S, args))
    if check and r.returncode:
        raise SnapshotError(f"git {args[0]} in {cwd.name} failed: {r.stderr.strip()[-300:]}")
    return r


def _refs(repo: Path, *patterns: str) -> dict[str, str]:
    out = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)", *patterns).stdout
    return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)


def _bundle(repo: Path, out: Path, revs: list[str]) -> bool:
    """A git bundle of `revs`; False when it would hold no commits."""
    r = _git(repo, "bundle", "create", str(out), *revs, check=False)
    if r.returncode == 0:
        return True
    if "empty bundle" in r.stderr:
        out.unlink(missing_ok=True)
        return False
    raise SnapshotError(f"git bundle in {repo.name} failed: {r.stderr.strip()[-300:]}")


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _db_copy(src: Path, out: Path) -> dict:
    """project.db.gz: a transactionally consistent copy (backup API, own connection), secrets dropped
    from kv, integrity-checked before it is compressed. Raises when the copy is damaged."""
    raw = out.with_name("project.db")
    s = sqlite3.connect(str(src), timeout=60)
    d = sqlite3.connect(str(raw))
    try:
        s.backup(d)
        last_event = s.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0] \
            if s.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone() else 0
    finally:
        s.close()
    try:
        d.execute("PRAGMA journal_mode=DELETE")
        d.execute("PRAGMA secure_delete=ON")   # deleted secrets are zeroed, whatever sqlite's build default
        if d.execute("SELECT 1 FROM sqlite_master WHERE name='kv'").fetchone():
            d.execute("DELETE FROM kv WHERE " + " OR ".join(["lower(key) LIKE ?"] * len(DENY_KV)), DENY_KV)
            d.commit()
        ok = d.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        d.close()
    if ok != "ok":
        raise SnapshotError(f"the database copy failed its integrity check ({ok}); nothing was shipped")
    sha = _sha(raw)
    with open(raw, "rb") as f, open(out, "wb") as g, gzip.GzipFile(fileobj=g, mode="wb", mtime=0) as z:
        shutil.copyfileobj(f, z, 1 << 20)
    raw.unlink()
    return {"sha256": sha, "last_event": last_event}


def _is_repo(path: Path) -> bool:
    return (path / ".git").exists()


def _task_refs(p: project.Project) -> dict[str, str]:
    """Open tasks' branches, branches still queued for delivery.backup_remote, refs/ttp/push/*."""
    if not _is_repo(p.root):
        return {}
    names = {r["branch"] for r in p.db.q(
        f"SELECT branch FROM tasks WHERE branch IS NOT NULL AND branch!='' AND status IN ({','.join('?' * len(OPEN))})",
        OPEN)}
    pending = p.db.kv("backup_pending") or {}
    names |= {v.get("branch") for v in pending.values() if isinstance(v, dict) and v.get("branch")}
    heads = _refs(p.root, "refs/heads/")
    out = {f"refs/heads/{b}": heads[f"refs/heads/{b}"] for b in sorted(names) if f"refs/heads/{b}" in heads}
    out.update(_refs(p.root, "refs/ttp/push/"))
    return out


def _run_files(p: project.Project) -> dict[str, Path]:
    """The latest run's hand-off, prompt and run record of each open task: {rel path: file}."""
    rows = p.db.q(f"SELECT r.task, r.dir FROM runs r JOIN tasks t ON t.id=r.task WHERE t.status IN "
                    f"({','.join('?' * len(OPEN))}) AND r.id=(SELECT MAX(id) FROM runs WHERE task=r.task)", OPEN)
    out = {}
    for r in rows:
        d = Path(r["dir"] or "")
        if not r["dir"] or not d.is_absolute():
            d = p.runs / str(r["dir"] or "")
        for name in RUN_FILES:
            f = d / name
            try:
                if f.is_file() and f.stat().st_size <= RUN_FILE_MAX:
                    out[f"runs/{r['task']}/{name}"] = f
            except OSError:
                pass
    return out


def _shared(p: project.Project, cfg: dict) -> dict:
    """This project's open machine-ledger changes and the pauses of the resources it names; `by`
    is kept, so a user's pause stays the user's."""
    from . import machine_ledger, shared
    try:
        changes = [c for c in machine_ledger.changes() if c.get("by") == p.name]
    except Exception:
        changes = []
    try:
        pauses = shared.paused(cfg)
    except Exception:
        pauses = {}
    return {"changes": changes, "pauses": pauses}


def _epoch(p: project.Project) -> int:
    """The lease epoch this host last wrote (state/lease.json), 0 without a lease."""
    try:
        return int(json.loads((p.state / "lease.json").read_text()).get("epoch") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def build(p: project.Project, cfg: dict, stage: Path, st: dict, seq: int, now: float) -> dict | None:
    """Write snapshot `seq` into `stage` and return its manifest; None when nothing changed since the
    last shipped one."""
    last = st.get("last") or {}
    stage.mkdir(parents=True)
    db = _db_copy(p.state / "project.db", stage / "project.db.gz")
    harness_refs = _refs(p.harness, "refs/heads/")
    head = _git(p.harness, "rev-parse", "HEAD").stdout.strip()
    branch = _git(p.harness, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip() or "main"
    task_refs = _task_refs(p)
    runs = _run_files(p)
    shared_doc = _shared(p, cfg)
    for rel, src in runs.items():
        (stage / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, stage / rel)
    project.write_json(stage / "shared.json", shared_doc)
    fp = hashlib.sha256(json.dumps([db["sha256"], harness_refs, task_refs, shared_doc,
                                    {rel: _sha(stage / rel) for rel in runs}], sort_keys=True).encode()).hexdigest()
    full = bool(st.get("need_full")) or not last or now - float(last.get("full_at") or 0) >= FULL_EVERY_S
    if not full and fp == last.get("fingerprint"):
        return None
    # differential against the last full snapshot, so a snapshot needs only itself and that one
    base = [] if full else sorted({v for v in (last.get("full_refs") or {}).values()
                                   if _git(p.harness, "cat-file", "-e", f"{v}^{{commit}}", check=False).returncode == 0})
    if not full and not base:
        full = True
    _bundle(p.harness, stage / "harness.bundle", ["--branches", *(["--not", *base] if base else [])])
    if task_refs:
        remotes = _git(p.root, "for-each-ref", "--count=1", "refs/remotes/").stdout.strip()
        _bundle(p.root, stage / "branches.bundle", [*task_refs, "--not", "--remotes" if remotes else "HEAD"])
    files = {}
    for f in sorted(x for x in stage.rglob("*") if x.is_file()):
        files[f.relative_to(stage).as_posix()] = {"sha256": _sha(f), "size": f.stat().st_size}
    manifest = {
        "format": 1, "identity": identity(p, cfg), "project": p.name, "source_host": project.hostname(),
        "seq": seq, "created": now, "epoch": _epoch(p), "chain_from": seq if full else int(last["chain_from"]),
        "fingerprint": fp, "db": db,
        "harness": {"head": head, "branch": branch, "refs": harness_refs, "full": full, "base": base},
        "branches": task_refs, "files": files}
    project.write_json(stage / "manifest.json", manifest)
    return manifest


def ship(cfg: dict, s: dict, ident: str, stage: Path, manifest: dict) -> dict:
    """Stream `stage` to the receiver, manifest last; returns its answer."""
    cmd = _receiver_cmd(cfg, s["dest"], "receive", ident, manifest["seq"], s["keep"], manifest["chain_from"])
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdin, mode="w|") as t:
            for rel in [*manifest["files"], "manifest.json"]:
                t.add(str(stage / rel), arcname=rel, recursive=False)
    except (BrokenPipeError, OSError):
        pass   # the receiver stopped reading: its answer says why
    with contextlib.suppress(OSError):
        proc.stdin.close()
    proc.stdin = None   # closed: communicate() must not flush it
    try:
        out, err = proc.communicate(timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return {"error": f"the transfer took over {TIMEOUT_S} s", "rc": -9}
    return _answer(out, err, proc.returncode)


def snapshot(p: project.Project, cfg: dict | None = None, now: float | None = None, mark: Any = None) -> dict:
    """Take and ship one snapshot. {"outcome": off|busy|unchanged|ok|failed, ...}; records it in
    state/replica.json. One at a time per project (state/replica.lock)."""
    cfg = p.config() if cfg is None else cfg
    s = settings(cfg)
    if not s:
        return {"outcome": "off"}
    now = time.time() if now is None else now
    lk = os.open(p.state / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"outcome": "busy"}
        st = load_state(p)
        seq = int(st.get("seq") or 0) + 1
        stage = p.state / "replica-stage"
        shutil.rmtree(stage, ignore_errors=True)
        t0 = time.monotonic()
        res: dict = {"seq": seq}
        try:
            ident = identity(p, cfg)
            man = build(p, cfg, stage, st, seq, now)
            if man is None:
                res["outcome"] = "unchanged"
                if st.get("last"):
                    st["last"]["current_at"] = now   # the standby still holds the current state
            else:
                size = sum(v["size"] for v in man["files"].values())
                ans = ship(cfg, s, ident, stage, man)
                if "error" in ans:
                    res.update(outcome="failed", error=ans["error"])
                    if ans.get("need_full") or ans.get("next"):
                        st["need_full"] = True
                    if ans.get("next"):
                        st["seq"] = int(ans["next"]) - 1
                else:
                    prev = st.get("last") or {}
                    st["last"] = {"seq": seq, "at": now, "size": size, "files": len(man["files"]),
                                  "secs": round(time.monotonic() - t0, 1), "fingerprint": man["fingerprint"],
                                  "harness_refs": man["harness"]["refs"], "chain_from": man["chain_from"],
                                  "full_at": now if man["harness"]["full"] else prev.get("full_at", now),
                                  "full_refs": man["harness"]["refs"] if man["harness"]["full"] else prev.get("full_refs"),
                                  "current_at": now,
                                  "kept": ans.get("kept")}
                    st["seq"], st["need_full"] = seq, False
                    res.update(outcome="ok", size=size, kept=ans.get("kept"), full=man["harness"]["full"])
        except Exception as e:
            res.update(outcome="failed", error=f"{type(e).__name__}: {e}"[:500])
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        st.update(at=now, outcome=res["outcome"], error=res.get("error"))
        if mark is not None:
            st["mark"] = mark
        _save_state(p, st)
        return res
    finally:
        os.close(lk)


# when, and how it looks ---------------------------------------------------------------------------
def due(st: dict, s: dict, now: float, mark: Any = None) -> bool:
    """A snapshot is due after the interval (a third of it after a failure), or soon after `mark`
    (the count of finished tasks) changed."""
    last = float(st.get("at") or 0)
    every = s["every_s"] / 3 if st.get("outcome") == "failed" else s["every_s"]
    if now - last >= max(MIN_GAP_S, every):
        return True
    return mark is not None and st.get("mark") is not None and mark != st.get("mark") and now - last >= MIN_GAP_S


def _ago(s: float) -> str:
    s = max(0, int(s))
    return f"{s} s" if s < 90 else f"{s // 60} min" if s < 5400 else f"{s / 3600:.1f} h"


def _current_at(st: dict) -> float:
    """When the standby was last known to hold the current state: a shipped snapshot, or a check that
    found nothing new since it; else when failover.standby was set."""
    last = st.get("last") or {}
    return float(last.get("current_at") or last.get("at") or st.get("since") or 0)


def stale_text(p: project.Project, cfg: dict, now: float) -> str:
    """What a stale snapshot looks like to the user ('' while fresh or off)."""
    s = settings(cfg)
    if not s:
        return ""
    st = load_state(p)
    ref = _current_at(st)
    if not ref or now - ref < s["stale_s"]:
        return ""
    what = f"the standby was last known to hold the current state {_ago(now - ref)} ago" if st.get("last") else \
        f"no snapshot has been shipped in the {_ago(now - ref)} since failover.standby was set"
    why = f"; last attempt: {st.get('error')}" if st.get("outcome") == "failed" and st.get("error") else ""
    return (f"standby {s['dest']}: {what} (limit {_ago(s['stale_s'])}){why}. A stand-in would start from "
            f"stale state; check `ttp replicate --verify`.")


def status_line(p: project.Project, cfg: dict, now: float) -> str:
    s = settings(cfg)
    if not s:
        return ""
    st = load_state(p)
    last = st.get("last")
    if last:
        line = (f"standby {s['dest']}: snapshot #{last['seq']} {_ago(now - float(last['at']))} old "
                f"({float(last.get('size') or 0) / 1e6:.1f} MB)")
        if now - _current_at(st) >= s["stale_s"]:
            line += ", STALE"
        elif _current_at(st) - float(last["at"]) >= 90:
            line += f", nothing new since (checked {_ago(now - _current_at(st))} ago)"
    else:
        line = f"standby {s['dest']}: no snapshot yet"
    if st.get("outcome") == "failed":
        line += f"; last attempt failed {_ago(now - float(st.get('at') or now))} ago: {str(st.get('error'))[:160]}"
    return line


# verify and restore -------------------------------------------------------------------------------
def verify(cfg: dict, ident: str, dest: str | None = None) -> dict:
    """The receiver's check of every snapshot at the destination: {"dir", "snapshots": [{seq, ok, why}]}."""
    dest = dest or (settings(cfg) or {}).get("dest")
    if not dest:
        return {"error": "failover.standby is not set"}
    try:
        r = subprocess.run(_receiver_cmd(cfg, dest, "verify", ident), capture_output=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"error": f"verifying took over {TIMEOUT_S} s"}
    return _answer(r.stdout, r.stderr, r.returncode)


def restore(src: str | Path, into: str | Path, seq: int | None = None) -> dict:
    """Make a project folder at `into`/tt-project from a verified snapshot under `src` (an identity
    folder, or one snapshot's folder). The database and harness are in place; the rest (task
    branches, runs, shared state, manifest) waits in state/standby-import/ for the stand-in step.
    Raises SnapshotError naming what is missing or damaged."""
    src = Path(src).expanduser()
    if (src / "manifest.json").is_file() and src.name.isdigit():
        seq, src = int(src.name) if seq is None else seq, src.parent
    v = verify({}, src.name, dest=str(src.parent.resolve()))
    if "error" in v:
        raise SnapshotError(v["error"])
    snaps = {x["seq"]: x for x in v.get("snapshots") or []}
    if seq is None:
        good = [k for k, x in snaps.items() if x["ok"]]
        if not good:
            raise SnapshotError(f"no complete snapshot in {src}" + (
                f" (newest #{max(snaps)}: {snaps[max(snaps)]['why']})" if snaps else ""))
        seq = max(good)
    if seq not in snaps or not snaps[seq]["ok"]:
        raise SnapshotError(f"snapshot #{seq} in {src} is not usable: {snaps.get(seq, {}).get('why') or 'not there'}")
    snap = src / f"{seq:06d}"
    man = json.loads((snap / "manifest.json").read_text())
    base = Path(into).expanduser() / project.FOLDER
    if base.exists() and any(base.iterdir()):
        raise SnapshotError(f"{base} exists and is not empty (a restore that was cut off leaves it so: "
                            "remove it and run the restore again)")
    state, harness = base / "state", base / "harness"
    state.mkdir(parents=True, exist_ok=True)
    with gzip.open(snap / "project.db.gz", "rb") as z, open(state / "project.db", "wb") as f:
        shutil.copyfileobj(z, f, 1 << 20)
    if _sha(state / "project.db") != man["db"]["sha256"]:
        raise SnapshotError("the restored database does not match its manifest")
    c = sqlite3.connect(str(state / "project.db"))
    try:
        ok = c.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        c.close()
    if ok != "ok":
        raise SnapshotError(f"the restored database failed its integrity check: {ok}")
    h = man["harness"]
    harness.mkdir()
    _git(harness, "init", "-q")
    for k in sorted({int(man["chain_from"]), seq}):
        b = src / f"{k:06d}" / "harness.bundle"
        if b.is_file():
            _git(harness, "fetch", "-q", "--update-head-ok", str(b), "+refs/heads/*:refs/heads/*")
    _git(harness, "checkout", "-q", "-f", h["branch"])
    _git(harness, "reset", "-q", "--hard", h["head"])
    imp = state / "standby-import"
    imp.mkdir()
    for rel in [*man["files"], "manifest.json"]:
        if rel.startswith("runs/") or rel in ("shared.json", "branches.bundle", "manifest.json"):
            (imp / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(snap / rel, imp / rel)
    return {"seq": seq, "project_dir": str(base), "harness_head": h["head"], "created": man["created"],
            "source_host": man.get("source_host"), "epoch": man.get("epoch", 0), "import": str(imp)}
