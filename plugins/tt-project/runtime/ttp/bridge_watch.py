"""`ttp bridge watch`: one pass of this computer's bridge health check (more during a fast window,
below), run by a service `ttp bridge setup` installs (launchd: every INTERVAL_S and on each network change; systemd: a user timer).

After a sleep or a network change, the box's sshd can keep the old session and its forwarded ports for
hours, so the new session fails on its forwards until someone restarts it by hand. Each pass asks every
box, in one login, whether each forwarded port answers end to end (a connection to it gets the first
bytes of the host's ssh banner). A port that does not answer gets the box cleanup (bridge_relay.CLEANUP:
it ends only this account's sshd session holding exactly that port, SIGTERM then SIGKILL) and its service
is restarted at once, skipping the random start delay. A box that cannot be reached (this computer is
offline, or the box is down) is never left to a network change alone: it is asked again within BACKOFF_S,
each attempt is logged, and its bridge services are restarted at most once per RESTART_GAP_S step — each
restart re-reads the ssh route and jump hosts and resolves the name again. Once it has stayed unreachable
ALERT_AFTER_S the user gets one desktop alert through the notifier, and one more note with the length of
the outage when the box answers again. No pass measures the time since the last one: a sleep longer than BACKOFF_S ends every backoff,
so the first pass after waking checks every box. A network change also opens a fast window: `ttp bridge
watch` checks again every FAST_STEP_S, ignoring the backoff, until every box answers and no service still
fails, for at most FAST_S; the steady interval and the backoff then apply again. A port whose forward's
own destination does not answer from here (its host is off) is not a stale forward: nothing is ended or restarted for it, and it is logged
once. Nor is a silent port of a bridge session another of whose ports answers: the session is live and its
target only slow; unless that port failed while its destination was off (a session rebuilt then, after a VPN
reconnect say, can stay broken for it): once the destination answers it gets one restart if it still fails,
owed until a restart really runs. Nor is a bridge session none of whose ports has a listener yet while its
login (its ssh, a relay reconnect included) is younger than LOGIN_S: the next pass decides. A service that
keeps failing is restarted at most once per RESTART_GAP_S step.

Besides the sessions `ttp bridge` installs, the config's "watch" list names ssh forward services made by
hand: {"label": the launchd label or systemd unit, "box": its ssh alias, "port": the box port it forwards},
optionally with "target" (the host:port it forwards to) and "interface" (the one this computer reaches it
through), so a forward whose target is off is left alone the same way. One with no target that still connects
but sends nothing after a restart is not restarted again until its check result changes or the network
changes (macOS; a change can leave the forward stale again): best effort.
Each heal (from the first failed check to the first answer again) is one JSON line in HEAL_LOG; `ttp bridge
status` shows the last few."""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .project import durable_append, durable_write

LABEL = "com.tt-project.bridge-watch"
INTERVAL_S = 30
NET_PATHS = {"darwin": ["/Library/Preferences/SystemConfiguration"]}   # launchd runs the pass when it changes
BACKOFF_S = (30, 60)                  # the next check of a box that could not be reached: never over a minute
RESTART_GAP_S = (0, 60, 120, 300)   # the least time between restarts of one service that keeps failing
ALERT_AFTER_S = 300                  # a box unreachable this long gets one desktop alert, one more when back
STALE_S = 3 * max(BACKOFF_S)         # a failed check this long after the last one (a sleep between) starts a new outage
#                                     (a restarted session needs some seconds to log in and forward its ports)
LOGIN_S = INTERVAL_S                # a session's login younger than this may not have set up its forwards yet
RESOLVE_S = 5                       # the bound on one destination name lookup before probing it
FAST_S, FAST_STEP_S = 120, 5        # after a network change: a check every FAST_STEP_S until all answer, at most FAST_S
STATE, HEAL_LOG = "bridge-watch.json", "bridge-heal.log"   # in the bridge home (~/.tt-project)


def _home() -> Path:
    from . import bridge
    return bridge.HOME_DIR

# On the box: per port `up` (a byte came), `down` (it connected, no byte: a stale forward, or a live one whose
# target is off) or `none` (no listener), then the done line; `nocheck` when it lacks the tools (nothing is restarted).
CHECK = r"""command -v bash >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1 || { echo ttp-watch-nocheck; exit 0; }
for p in "$@"; do   # all ports at once, so the check takes one probe's time whatever their number
  ( b=$(timeout 6 bash -c 'exec 3<>"/dev/tcp/127.0.0.1/$0" || exit 7; head -c 1 <&3' "$p" 2>/dev/null); rc=$?
    if [ -n "$b" ]; then echo "up $p"; elif [ $rc = 7 ]; then echo "none $p"; else echo "down $p"; fi ) &
done
wait
echo ttp-watch-done"""


def targets(cfg: dict, platform: str | None = None) -> list[dict]:
    """The installed bridge sessions and the config's watched services: {service, box, ports, agent}."""
    from . import bridge
    out = []
    for box, b in cfg["boxes"].items():
        for an, a in b["agents"].items():
            fw = [h for h in a["forward"] if not h["adopt"]]
            if fw and bridge.service_file(box, an, platform).is_file():
                dests = {h["port"]: h for h in fw}
                out.append({"service": bridge.label(box, an), "box": box, "ports": list(dests), "agent": an,
                            "dests": dests})
    for w in cfg.get("watch", []):
        t = {"service": w["label"], "box": w["box"], "ports": [w["port"]], "agent": ""}
        if w.get("target"):
            t["dests"] = {w["port"]: {"target": w["target"], "to": w["to"], "interface": w["interface"]}}
        out.append(t)
    return out


def _load() -> dict:
    try:
        d = json.loads((_home() / STATE).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _net_stamp(platform: str) -> float:
    m = 0.0
    for p in NET_PATHS.get(platform, []):
        try:
            m = max(m, os.stat(p).st_mtime)
        except OSError:
            pass
    return m


def dest_answers(h: dict) -> bool:
    """A forward's target answers (on port 22 unless "to" names another) from this computer, by the route the
    relay itself takes (its name resolution is bounded: a destination whose name hangs a lookup here
    is never waited on)."""
    from . import bridge_relay
    try:
        s = bridge_relay.bound_connect(h["target"], h["interface"], h.get("to", 22), timeout=5)
        s.close()
        return True
    except (OSError, ValueError):
        return False


def dest_resolves(h: dict) -> bool:
    """Whether a destination's name resolves from here within a bound: one that does not is never
    probed, so a probe can never set off the frozen name lookup of a raw ssh forward."""
    from . import bridge_relay
    try:
        bridge_relay._resolve(h["target"], h.get("to", 22), RESOLVE_S)
        return True
    except OSError:
        return False


def _alert(title: str, body: str) -> None:
    """One desktop notification through the existing notifier route; a failure never breaks a pass."""
    try:
        from .notifier import show
        show(f"tt-project: {title}", body)
    except Exception as e:   # the watch pass must survive a failed alert
        print(f"alert failed: {e}", file=sys.stderr)


def _gap(restarts: int) -> float:
    """The least time before the next restart of something restarted `restarts` times already."""
    return RESTART_GAP_S[min(restarts, len(RESTART_GAP_S) - 1)]


def _down_alert(box: str, bs: dict, now: float) -> str | None:
    """One desktop alert for a box that has stayed unreachable ALERT_AFTER_S; the state marks it sent."""
    if bs.get("alert") or not bs.get("since") or now - bs["since"] < ALERT_AFTER_S:
        return None
    bs["alert"] = now
    _alert(f"{box} unreachable", f"{box} has been unreachable for {int(now - bs['since'])}s; "
                                 "its bridges are retried all the while.")
    return f"{box}: unreachable for {int(now - bs['since'])}s; user alerted"


def _recovered(box: str, bs: dict, now: float) -> str | None:
    """The recovery note for an outage that was long enough to alert: one desktop note with its length."""
    if not bs.get("alert"):
        return None
    secs = int(now - bs.get("since", now))
    _alert(f"{box} reachable again", f"{box} answers again after {secs}s unreachable.")
    return f"{box}: reachable again after {secs}s"


def release(cfg: dict, t: dict, ports: list[int]) -> bool:
    """The box cleanup for the ports that did not answer; True once they are free."""
    from . import bridge, bridge_relay
    if t["agent"]:
        ssh, prefix = bridge.ssh_argv(cfg, t["box"], t["agent"]), f"{t['box']}/{t['agent']}/"
    else:   # a hand-made service: no tag or record of ours, so only a session with no command of its own is ended
        ssh, prefix = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ControlMaster=no", "-o", "ControlPath=none",
                       "--", bridge.sshcmd.check_host(t["box"]), "true"], "watch/"
    d = bridge.relay_dir(t["box"], t["agent"] or "watch")
    d.parent.mkdir(parents=True, exist_ok=True)   # computer_id keeps its file beside the relay dirs
    cid = bridge_relay.computer_id(str(d))
    return bridge_relay.clean(ssh, f"{cid}/{prefix}", ports)


def _unit(t: dict) -> str:
    return t["service"] if t["service"].endswith(".service") else t["service"] + ".service"


def loaded(t: dict, platform: str) -> bool:
    """The service is meant to run: loaded in launchd, or active, restarting or failed (its start limit hit) in
    systemd (`ttp bridge stop`, or a hand-made service someone stopped, is left stopped)."""
    from . import bridge, service
    if platform == "darwin":
        return service.launchd_loaded(t["service"], lambda *a: bridge._run(list(a)))
    r = bridge._run(["systemctl", "--user", "is-active", "--", _unit(t)])
    return r.stdout.strip() in ("active", "activating", "reloading", "failed")


def restart(t: dict, platform: str) -> str:
    from . import bridge
    if t["agent"]:
        m = bridge.bridge_marker(t["box"], t["agent"])   # skip the random start delay
        m.parent.mkdir(parents=True, exist_ok=True)
        m.touch()
    if platform == "darwin":
        r = bridge._run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{t['service']}"])
    else:
        bridge._run(["systemctl", "--user", "reset-failed", "--", _unit(t)])   # a hit start limit refuses a restart
        r = bridge._run(["systemctl", "--user", "restart", "--", _unit(t)])
    return "restarted" if r.returncode == 0 else f"restart refused: {(r.stderr or r.stdout).strip()[:160]}"


def login_age(t: dict, platform: str) -> int | None:
    """Seconds since the bridge session's ssh started (its service's newest ssh child: the relay's current login);
    None when unknown, or when it has none yet (its random start wait, which a restart skips)."""
    from . import bridge
    if platform == "darwin":
        r = bridge._run(["launchctl", "print", f"gui/{os.getuid()}/{t['service']}"])
        pid = re.search(r"^\s*pid = (\d+)$", r.stdout, re.M)
    else:
        r = bridge._run(["systemctl", "--user", "show", "-p", "MainPID", "--value", "--", _unit(t)])
        pid = re.fullmatch(r"\s*([1-9]\d*)\s*", r.stdout)
    ssh = pid and bridge._run(["pgrep", "-n", "-x", "-P", pid.group(1), "ssh"]).stdout.split()
    if not ssh:
        return None
    m = re.fullmatch(r"\s*(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)\s*", bridge._run(["ps", "-o", "etime=", "-p", ssh[0]]).stdout)
    if not m:
        return None
    d, h, mi, sec = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + sec


def run_pass(cfg: dict, platform: str | None = None, now: float | None = None) -> list[str]:
    """One pass: what it did, one line per action (nothing when all answer)."""
    from . import bridge
    platform, now = platform or sys.platform, time.time() if now is None else now
    st, out = _load(), bridge.hold_pass(cfg, platform)   # first: a held box's sessions go, and targets() with them
    boxes, fails, off = st.setdefault("boxes", {}), st.setdefault("fail", {}), st.setdefault("unreachable", {})
    net = _net_stamp(platform)
    event = "network change" if net and st.get("net") and net > st["net"] else ""
    st["net"] = net
    if event:   # a network change can leave any forward stale again
        st["fast_until"] = now + FAST_S
        for f in fails.values():
            f.pop("down", None)
    fast, waiting = st.get("fast_until", 0) > now, False
    ts = targets(cfg, platform)
    closed = bridge.held_boxes({t["box"] for t in ts})   # a hand-made watch entry into a held box: no ssh to it either
    for box in dict.fromkeys(t["box"] for t in ts):
        if box in closed:
            continue
        mine = [t for t in ts if t["box"] == box]
        bs = boxes.get(box, {})
        if bs.get("next", 0) > now and not fast:
            continue
        ports = sorted({p for t in mine for p in t["ports"]})
        # every destination resolves before anything is probed: one whose name does not resolve here
        # is never probed at all (connecting to its forwarded port could hang a raw ssh forward on
        # the same lookup) and never counted as a dead link
        probes = [(t["service"], p, t["dests"][p]) for t in mine if t.get("dests") for p in t["dests"]]
        with ThreadPoolExecutor(max(1, len(probes))) as ex:
            resolves = dict(zip([n[:2] for n in probes], ex.map(dest_resolves, [n[2] for n in probes])))
        unresolved = {sp for sp, ok in resolves.items() if not ok}
        ports = [p for p in ports if not any((t["service"], p) in unresolved for t in mine)]
        r = bridge._box(box, "sh -c " + shlex.quote(CHECK) + " ttp-watch " + " ".join(map(str, ports)), timeout=40)
        if "ttp-watch-done" not in r.stdout:
            if "ttp-watch-nocheck" in r.stdout:   # it answered; it only lacks the tools, nothing is restarted
                boxes.pop(box, None)
                if (rec := _recovered(box, bs, now)):
                    out.append(rec)
                continue
            # offline or the box is down: keep checking, keep restarting its bridges (never only a
            # network change): each restart re-reads the ssh route and jump hosts and resolves the
            # name again, paced like any other failing service; the user is told once
            if not bs.get("alert") and now - bs.get("at", now) > STALE_S:
                bs = {}   # its last failed check was before a sleep: the outage is counted from this check
            n = bs.get("n", 0) + 1
            wait = BACKOFF_S[min(n, len(BACKOFF_S)) - 1]
            due = now - bs.get("last", 0) >= _gap(bs.get("restarts", 0))
            live = [t for t in mine if loaded(t, platform)] if due else []
            acts = [f"{t['service']}: {restart(t, platform)}" for t in live]
            boxes[box] = {"n": n, "at": now, "next": now + wait, "since": bs.get("since") or now,
                          "alert": bs.get("alert", 0), "restarts": bs.get("restarts", 0) + bool(acts),
                          "last": now if acts else bs.get("last", 0)}
            out.append(f"{box}: not reachable; " + ("restarted its bridges to retry the route"
                       if acts else f"next check in {wait}s or on a network change"))
            if refused := [a for a in acts if not a.endswith(": restarted")]:
                out.append(f"{box}: some restarts refused: {'; '.join(refused)}")
            if line := _down_alert(box, boxes[box], now):   # only after a check that failed: a sleep never alerts
                out.append(line)
            continue
        boxes.pop(box, None)
        if (rec := _recovered(box, bs, now)):
            out.append(rec)
        down = {int(ln.split()[1]) for ln in r.stdout.splitlines() if ln.startswith("down ")}
        none = {int(ln.split()[1]) for ln in r.stdout.splitlines() if ln.startswith("none ")}
        up = {int(ln.split()[1]) for ln in r.stdout.splitlines() if ln.startswith("up ")}
        down |= {p for t in mine for p in t["ports"] if (t["service"], p) in unresolved}
        # a bridge session holds all its forwards or exits (ExitOnForwardFailure): one answering port shows it live,
        # so a silent one whose target answers is only slow there, and a restart would only drop the others
        live = {t["service"] for t in mine if t["agent"] and up & set(t["ports"])}
        # a port with no listener is dead whatever its target does; one that connects but sends nothing may be a
        # live forward whose target is off: its targets are asked all at once
        ask = [(t["service"], p, t["dests"][p]) for t in mine if t.get("dests") for p in t["ports"]
               if p in down and (t["service"], p) not in unresolved]
        with ThreadPoolExecutor(max(1, len(ask))) as ex:
            answers = dict(zip([a[:2] for a in ask], ex.map(dest_answers, [a[2] for a in ask])))
        answers.update({sp: False for sp in unresolved})   # never asked: its name does not resolve here
        for t in mine:
            # a port that failed while its destination was off is not spared once it answers: a session (re)built
            # then can stay broken for it, so it gets one restart if the port still fails
            was_off = set(off.get(t["service"], []))
            dead = [p for p in t["ports"] if p in none or p in down
                    and not (t["service"] in live and answers[t["service"], p] and p not in was_off)]
            f = fails.get(t["service"])
            bad = [p for p in dead if p in none or answers[t["service"], p]] if t.get("dests") else dead
            gone = [p for p in dead if p not in bad]   # the forward is up; its destination does not answer
            owed = [p for p in bad if p in was_off]    # its one restart is owed: kept marked until it runs
            if gone and [p for p in off.get(t["service"], []) if p not in owed] != gone:
                out.append(f"{t['service']}: port {', '.join(map(str, gone))} on {box}: its destination does not "
                           "answer from here; nothing restarted")
            off[t["service"]] = [p for p in t["ports"] if p in gone or p in owed]
            if not bad:
                if gone:
                    continue
                if f:
                    rec = {"service": t["service"], "box": box, "failed_at": f["at"], "event": f["event"],
                           "healed_at": now, "seconds": round(now - f["at"]), "restarts": f["restarts"]}
                    _home().mkdir(parents=True, exist_ok=True)
                    durable_append(_home() / HEAL_LOG, json.dumps(rec) + "\n")
                    out.append(f"{t['service']}: answers again after {rec['seconds']}s")
                    del fails[t["service"]]
                continue
            if not loaded(t, platform):
                fails.pop(t["service"], None)
                continue
            if not t.get("dests") and f and f["restarts"] and bad == f.get("down") == [p for p in bad if p in down]:
                # no target to ask: still connecting with nothing to say after a restart may be a live forward whose
                # target is off, so it waits for its check result to change
                if not f.get("said"):
                    out.append(f"{t['service']}: port {', '.join(map(str, bad))} on {box} still does not answer after "
                               "a restart; its watch entry names no target to ask, so it is not restarted again "
                               "until its check changes")
                    f["said"] = True
                continue
            if f and now - f["last"] < _gap(f["restarts"]):
                continue
            # a login under way is no failure yet: a spared session that comes up records no heal
            if t["agent"] and none >= set(t["ports"]) and (age := login_age(t, platform)) is not None and age < LOGIN_S:
                out.append(f"{t['service']}: still logging in ({age}s); not restarted this pass")
                waiting = True
                continue
            f = fails.setdefault(t["service"], {"at": now, "event": event or ("network change" if fast else ""),
                                                "restarts": 0, "last": 0})
            freed = release(cfg, t, bad)
            said = restart(t, platform)
            if said == "restarted":   # a refused restart leaves the owed one marked
                off[t["service"]] = gone
            f.update(restarts=f["restarts"] + 1, last=now, down=[p for p in bad if p in down], said=False)
            out.append(f"{t['service']}: port {', '.join(map(str, bad))} on {box} did not answer"
                       f"{'' if freed else ' (still held after the cleanup)'}; {said}")
    names = {t["service"] for t in ts}
    st["fail"] = {k: v for k, v in fails.items() if k in names}
    st["unreachable"] = {k: v for k, v in off.items() if k in names and v}
    # the fast window ends once every box answers and no service still fails (one held for its check to change aside)
    if not fast or not (any(t["box"] in boxes for t in ts) or waiting or any(not f.get("said") for f in st["fail"].values())):
        st.pop("fast_until", None)
    _home().mkdir(parents=True, exist_ok=True)
    durable_write(_home() / STATE, json.dumps(st))
    return out


def run_window(cfg: dict, platform: str | None = None, clock=time.time, sleep=time.sleep):
    """`ttp bridge watch`: a pass, then one every FAST_STEP_S while a network change's fast window stays open;
    yields each pass's lines as it ends."""
    while True:
        yield from run_pass(cfg, platform, clock())
        if _load().get("fast_until", 0) <= clock() + FAST_STEP_S:
            return
        sleep(FAST_STEP_S)


def heal_lines(platform: str | None = None, n: int = 3) -> list[str]:
    """`ttp bridge status`'s lines on the watch: installed or not, and the last n heals."""
    try:
        rows = [json.loads(ln) for ln in (_home() / HEAL_LOG).read_text().splitlines()[-n:] if ln.strip()]
    except (OSError, ValueError):
        rows = []
    out = [f"self-heal watch: {'installed' if service_files(platform)[0].is_file() else 'not installed (`ttp bridge setup`)'}"]
    for r in rows:
        out.append(f"  healed {r['service']} on {r['box']}: down at {time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(r['failed_at']))}"
                   f"{' after a ' + r['event'] if r.get('event') else ''}, answering again {r['seconds']}s later"
                   f" ({r['restarts']} restart(s))")
    return out


def argv() -> list[str]:
    from .release import service_runtime
    return ["/usr/bin/env", f"PYTHONPATH={service_runtime()}", sys.executable, "-m", "ttp", "bridge", "watch"]


def service_files(platform: str | None = None) -> list[Path]:
    if (platform or sys.platform) == "darwin":
        return [Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"]
    from . import hoststate
    return [hoststate.unit_dir() / f"{LABEL}.service", hoststate.unit_dir() / f"{LABEL}.timer"]


def contents(platform: str) -> list[bytes]:
    log = str(_home() / "bridge-watch.log")
    if platform == "darwin":
        import plistlib
        return [plistlib.dumps({"Label": LABEL, "ProgramArguments": argv(), "RunAtLoad": True,
                                "StartInterval": INTERVAL_S, "WatchPaths": NET_PATHS["darwin"],
                                "ProcessType": "Background", "StandardOutPath": log, "StandardErrorPath": log})]
    from .tunnel import _unit_escape
    return [(f"[Unit]\nDescription=tt-project: bridge health check\n\n[Service]\nType=oneshot\n"
             f"ExecStart={_unit_escape(' '.join(shlex.quote(a) for a in argv()))}\n").encode(),
            (f"[Unit]\nDescription=tt-project: bridge health check every {INTERVAL_S}s\n\n[Timer]\n"
             f"OnBootSec={INTERVAL_S}\nOnUnitActiveSec={INTERVAL_S}\nAccuracySec=5\n\n"
             "[Install]\nWantedBy=timers.target\n").encode()]


def install(platform: str | None = None) -> str:
    """Install or update the watch service; only what changed is rewritten and reloaded."""
    from . import bridge
    platform = platform or sys.platform
    files, data = service_files(platform), contents(platform)
    if all(f.is_file() and f.read_bytes() == d for f, d in zip(files, data)):
        return "unchanged"
    _home().mkdir(parents=True, exist_ok=True)
    for f, d in zip(files, data):
        f.parent.mkdir(parents=True, exist_ok=True)
        durable_write(f, d)
    if platform == "darwin":
        from .service import _reload
        r = _reload(LABEL, files[0], lambda *a: bridge._run(list(a)))
        return "launchd agent started" if r.returncode == 0 else f"launchd refused it: {(r.stderr or r.stdout).strip()[:200]}"
    bridge._run(["systemctl", "--user", "daemon-reload"])
    r = bridge._run(["systemctl", "--user", "enable", "--now", "--", f"{LABEL}.timer"])
    return "systemd user timer started" if r.returncode == 0 else f"systemd refused it: {r.stderr.strip()[:200]}"
