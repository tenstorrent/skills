"""setup-box: the user's claude.ai account connectors on this machine and each box: `box.py connectors check`.

- claude.ai connectors (Atlassian, Gmail, Google Calendar, Drive, Docs, Sheets, Slides, ...) belong to the
  claude.ai account. They are authorized once per account (claude.ai Settings > Connectors), and every Claude Code
  signed into that account lists them (`claude mcp list`: Connected or Needs authentication). A machine signed
  into another account cannot show them.
- `check [<alias> ...]` probes this machine, then each box over ssh, read-only: whether Claude Code is signed in,
  whether to the same account as this machine, and each connector of the families as connected, needs
  authentication, failed or missing. Only `same` or `different` leaves the comparison: the account's email and
  organization are hashed in memory, never printed or stored; no token or credential file is read.
- Families: Atlassian and Google by default; the site file's top-level `connectors` ({"<family>": ["<connector>",
  ...]}) or `--family <family>=<connector>,...` add more or extend one.
- `--verify` makes one real read-only call per family on each machine that shows it connected (`claude -p` on a
  small model and budget, only that connector's search, fetch, get, list, read, find, query or lookup tools, held
  to them by a PreToolUse hook). Only pass or fail, the tool's name and an item count leave the machine.
  Headless sessions load claude.ai connectors asynchronously, so neither `claude mcp list`'s Connected nor an early
  needs-auth proves anything: the call waits for MCP connections (MCP_CONNECTION_NONBLOCKING=false, MCP_TIMEOUT)
  and, when no tool of the family loaded, tries once more, all within VERIFY_CAP_S per machine. A connector whose
  tools still did not load is "connected, not loaded in headless runs" (a note, no Settings step); only an auth
  error from a connector call after the wait is "needs authentication" with the reconnect step.
- It never logs in or out, switches accounts or adds a connector: each finding names the one step that fixes it,
  and an account switch only on the user's yes.
Exit 0 all set, 1 a finding, 255 ssh to a box failed.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import shlex
import subprocess
import sys
from typing import Dict, List, Optional, Sequence

import box
import box_agents

RUNTIME = pathlib.Path(__file__).resolve().parents[3] / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))
from ttp import mcpgap  # noqa: E402  (the same `claude mcp list` reader as the MCP-gap status line)

BOX_PY = box_agents.BOX_PY
HERE_NAME = "here"
# Generic defaults; a family's first connected connector is the one --verify calls.
FAMILIES: Dict[str, List[str]] = {
    "Atlassian": ["Atlassian"],
    "Google": ["Google Drive", "Gmail", "Google Calendar", "Google Docs", "Google Sheets", "Google Slides"],
}
STATE_WORDS = {"connected": "connected", "needs_auth": "needs authentication", "failed": "failed to connect",
               "missing": "missing"}
CONNECTORS_OFF_VAR = "ENABLE_CLAUDEAI_MCP_SERVERS"
SETTINGS_STEP = "claude.ai Settings > Connectors"
READ_VERBS = ("search", "fetch", "get", "list", "read", "find", "query", "lookup")
VERIFY_MODEL = "haiku"
VERIFY_BUDGET_USD = "0.25"
VERIFY_TURNS = "6"
# A connector's read tool: `mcp__claude_ai_<name>__<verb>...`, no `__` inside the name.
READ_TOOL = "mcp__claude_ai_[A-Za-z0-9]+(_[A-Za-z0-9]+)*__(" + "|".join(READ_VERBS) + ")[A-Za-z0-9]*(_[A-Za-z0-9]+)*"
_T = 'T=; command -v timeout >/dev/null 2>&1 && T="timeout 150"; '
VERIFY_CAP_S = 120         # every --verify call on one machine, the retry included
VERIFY_MCP_TIMEOUT_MS = 60000
VERIFY_RETRY_PAUSE_S = 15  # before the one retry of a family whose tools did not load
# A connector call's error that means its authorization, not a missing or slow tool.
AUTH_ERROR = "(401|403|unauthori[sz]ed|unauthenticated|authenticat|re-?authori[sz]e|invalid[_ ]token|expired)"
NOT_LOADED = "connected, not loaded in headless runs"
HEADLESS_NOTE = ("note: claude.ai connectors can show Connected in `claude mcp list` yet load no tools in a headless "
                 "`claude -p` run, as tt-project workers run; workers then use other configured MCP tools. Nothing to "
                 "fix in Settings.")


def probe_script() -> str:
    """Read-only shell: `cauth=` the CLI's sign-in state (`claude auth status --json`, one line), then `claude mcp
    list` between `cmcp=begin` and `cmcp=end <rc>`. No token or credential file is read."""
    return (_T + box_agents._find("claude")
            + 'if [ -z "$b" ]; then echo cauth=none; exit 0; fi; '
            f'echo "coff=${{{CONNECTORS_OFF_VAR}:-}}"; '
            'echo "cauth=$($T "$b" auth status --json </dev/null 2>/dev/null | tr -d "\\n")"; '
            'echo cmcp=begin; (cd "$HOME" && $T "$b" mcp list </dev/null 2>&1); echo "cmcp=end $?"')


def _hash(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def read_probe(out: str) -> dict:
    """{"claude", "signed_in", "method", "account", "org", "listed", "rc", "states", "off"}. `account` and `org`
    are hashes, so nothing that names the account is kept."""
    lines = out.splitlines()
    auth = next((ln[6:] for ln in lines if ln.startswith("cauth=")), "")
    p: dict = {"claude": auth != "none" and any(ln.startswith("cauth=") for ln in lines), "signed_in": None,
               "method": "", "account": "", "org": "", "listed": False, "rc": None, "states": {},
               "off": any(ln.strip().lower() in ("coff=false", "coff=0") for ln in lines)}
    try:
        j = json.loads(auth) if auth and auth != "none" else None
    except ValueError:
        j = None
    if isinstance(j, dict):
        p["signed_in"] = j.get("loggedIn") is True
        p["method"] = str(j.get("authMethod") or "")
        org, email = str(j.get("orgId") or ""), str(j.get("email") or "").lower()
        p["org"] = _hash(org) if org else ""
        p["account"] = _hash(org, email) if org or email else ""
    if "cmcp=begin" in lines:
        i = lines.index("cmcp=begin")
        end = next((n for n in range(i + 1, len(lines)) if lines[n].startswith("cmcp=end")), None)
        body = lines[i + 1:end] if end is not None else lines[i + 1:]
        if end is not None:
            rc = lines[end].split()[-1]
            p["rc"] = int(rc) if rc.lstrip("-").isdigit() else None
        p["listed"] = p["rc"] == 0
        p["states"] = {k.lower(): v for k, v in mcpgap.states("\n".join(body)).items()
                       if k.lower().startswith(mcpgap.CONNECTOR_PREFIXES)}
    return p


def connector_state(p: dict, name: str) -> str:
    return p["states"].get(f"claude.ai {name}".lower(), "missing")


def families(site: Optional[dict] = None, extra: Sequence[str] = ()) -> Dict[str, List[str]]:
    """The defaults, then the site file's `connectors`, then each `--family <family>=<a>,<b>`; a family named
    again gains its new connectors."""
    out = {k: list(v) for k, v in FAMILIES.items()}
    adds: List[tuple] = []
    conf = (site or {}).get("connectors")
    if conf is not None:
        if not isinstance(conf, dict) or not all(isinstance(v, list) and all(isinstance(x, str) and x for x in v)
                                                 for v in conf.values()):
            raise ValueError('the site file\'s connectors must map a family to connector names: '
                             '{"<family>": ["<connector>", ...]}')
        adds += list(conf.items())
    for f in extra:
        fam, sep, names = f.partition("=")
        if not sep or not fam.strip() or not names.strip():
            raise ValueError(f"--family {f!r}: use <family>=<connector>,<connector>")
        adds.append((fam.strip(), [n.strip() for n in names.split(",") if n.strip()]))
    for fam, names in adds:
        have = out.setdefault(fam, [])
        have += [n for n in names if n not in have]
    return out


def tool_prefix(name: str) -> str:
    """`mcp__claude_ai_<name>__`: the connector's tool names, as Claude Code forms them."""
    return "mcp__claude_ai_" + re.sub(r"[^A-Za-z0-9]", "_", name) + "__"


def hook_settings() -> str:
    """--settings JSON: a PreToolUse hook that lets through only ToolSearch and a connector's read tools."""
    allow = '"tool_name" *: *"(ToolSearch|' + READ_TOOL + ')"'
    cmd = (f"grep -Eiq '{allow}' || {{ echo 'connector verify is read-only: only "
           + ", ".join(READ_VERBS) + " tools' >&2; exit 2; }")
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}]}})


def verify_prompt(names: Sequence[str]) -> str:
    which = ", ".join(f"{n} (tools starting {tool_prefix(n)})" for n in names)
    return (f"Use one claude.ai connector of these, in this order of preference, whose tools are available: {which}. "
            "Make exactly one read-only call: a search or list for Tenstorrent, at most 5 results. If the tools are "
            "not loaded yet, find them with ToolSearch first. Call no other tool and quote nothing it returns. Then "
            "reply with one line only: CONNECTOR_OK=<number of items returned>, or CONNECTOR_ERROR=<short reason> "
            "if no such tool is available or it fails.")


def verify_script(targets: Dict[str, List[str]]) -> str:
    """Shell: for each family, one `claude -p` on its connected connectors that waits for MCP connections, and once
    more after VERIFY_RETRY_PAUSE_S when none of their tools loaded, all within VERIFY_CAP_S. It prints
    `verify_<n>=<tool called>|<answer>`, `headless_<n>=` each connector's state in the init event, `loaded_<n>=` each
    connector's tool count there (empty: no init event), `autherr_<n>=1` when a connector call answered an auth error
    and `tries_<n>=`. The run's output stays in a shell variable on the machine: only these are printed."""
    s = (box_agents._find("claude") + 'cd "$HOME" || exit 1; TO=; command -v timeout >/dev/null 2>&1 && TO=timeout; '
         f'end=$(( $(date +%s) + {VERIFY_CAP_S} )); ')
    for n, names in enumerate(targets.values()):
        argv = ["-p", verify_prompt(names), "--model", VERIFY_MODEL, "--max-budget-usd", VERIFY_BUDGET_USD,
                "--max-turns", VERIFY_TURNS, "--output-format", "stream-json", "--verbose",
                "--allowedTools", *[tool_prefix(x)[:-2] for x in names], "ToolSearch", "--settings", hook_settings()]
        count = ''.join(f'$(printf "%s" "$i" | grep -o \'"{tool_prefix(x)}\' | wc -l | tr -d " "),' for x in names)
        s += ('t=0; c=; a=; h=; l=; e=; while [ "$t" -lt 2 ]; do left=$(( end - $(date +%s) )); '
              '[ "$left" -gt 5 ] || break; t=$((t + 1)); '
              f'o=$(MCP_CONNECTION_NONBLOCKING=false MCP_TIMEOUT={VERIFY_MCP_TIMEOUT_MS} $TO ${{TO:+$left}} '
              f'"$b" {shlex.join(argv)} </dev/null 2>/dev/null); '
              'i=$(printf "%s\\n" "$o" | grep -m 1 \'"subtype":"init"\'); '
              f'c=$(printf "%s" "$o" | grep -oiE \'"name":"{READ_TOOL}"\' | head -n 1 | cut -d\'"\' -f4); '
              'a=$(printf "%s" "$o" | grep -oE "CONNECTOR_(OK=[0-9]+|ERROR=[A-Za-z0-9 _.,:()-]{0,80})" | tail -n 1); '
              'h=$(printf "%s" "$i" | grep -oE \'"name":"claude\\.ai [^"]*","status":"[^"]*"\' '
              '| sed \'s/"name":"claude\\.ai \\([^"]*\\)","status":"\\([^"]*\\)"/\\1:\\2/\' | paste -sd ";" -); '
              f'l=; [ -n "$i" ] && l="{count[:-1]}"; '
              'e=; printf "%s\\n" "$o" | grep \'"type":"user"\' | grep \'"is_error":true\' '
              f'| grep -Eiq \'{AUTH_ERROR}\' && e=1; '
              '[ -n "$c" ] && break; case ",$l," in *,[1-9]*) break;; esac; '
              f'[ "$t" -lt 2 ] && [ $(( end - $(date +%s) )) -gt {VERIFY_RETRY_PAUSE_S * 2} ] '
              f'&& sleep {VERIFY_RETRY_PAUSE_S}; done; '
              f'echo "headless_{n}=$h"; echo "loaded_{n}=$l"; echo "autherr_{n}=$e"; echo "tries_{n}=$t"; '
              f'echo "verify_{n}=$c|$a"; ')
    return s + "true"


def read_verify(out: str, targets: Dict[str, List[str]]) -> Dict[str, dict]:
    """{family: {"ok", "tool", "items", "error", "headless", "loaded", "auth_error", "tries"}}: pass only when one of
    the family's connector tools was called and answered OK; `headless` maps each of its connectors to the state the
    headless session's init event showed, `loaded` to its tool count there (absent: no init event)."""
    keys = ("verify_", "headless_", "loaded_", "autherr_", "tries_")
    kv = dict(ln.split("=", 1) for ln in out.splitlines() if ln.startswith(keys) and "=" in ln)
    res: Dict[str, dict] = {}
    for n, (fam, names) in enumerate(targets.items()):
        tool, _, ans = kv.get(f"verify_{n}", "|").partition("|")
        ans = ans.strip()
        mine = tool if any(tool.startswith(tool_prefix(x)) for x in names) else ""
        ok = re.fullmatch(r"CONNECTOR_OK=(\d+)", ans)
        err = ans[len("CONNECTOR_ERROR="):] if ans.startswith("CONNECTOR_ERROR=") else ""
        seen = dict(x.split(":", 1) for x in kv.get(f"headless_{n}", "").split(";") if ":" in x)
        counts = kv.get(f"loaded_{n}", "").split(",")
        loaded = {x: int(c) for x, c in zip(names, counts) if c.strip().isdigit()} if len(counts) == len(names) else {}
        tries = kv.get(f"tries_{n}", "")
        res[fam] = {"ok": bool(mine and ok), "tool": mine, "items": int(ok.group(1)) if ok else None,
                    "error": err or ("no connector tool was called" if ok and not mine else "" if ok else "no answer"),
                    "headless": {x: seen[x] for x in names if x in seen}, "loaded": loaded,
                    "auth_error": bool(mine) and kv.get(f"autherr_{n}", "").strip() == "1",
                    "tries": int(tries) if tries.isdigit() else None}
    return res


def verify_targets(p: dict, fams: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """{family: its connectors `claude mcp list` shows connected}, for each family that has one."""
    out = {}
    for fam, names in fams.items():
        up = [n for n in names if connector_state(p, n) == "connected"]
        if up:
            out[fam] = up
    return out


def report(machine: str, p: dict, ref: Optional[dict], fams: Dict[str, List[str]],
           verified: Optional[Dict[str, dict]] = None) -> "tuple[List[str], List[str], List[str]]":
    """(lines, problems, fixes) for one machine. `ref` is this machine's probe (None: `machine` is this one)."""
    lines: List[str] = []
    problems: List[str] = []
    fixes: List[str] = []
    here = ref is None
    login = "claude auth login" if here else f"{BOX_PY} agents login {machine} claude"

    def find(problem: str, fix: str) -> None:
        problems.append(f"{machine}: {problem}")
        if fix not in fixes:
            fixes.append(fix)
    if not p["claude"]:
        find("Claude Code not installed", box_agents.CLIS["claude"]["install"] if here
             else f"{BOX_PY} agents install {machine} --cli claude --run")
        return [f"{machine}: Claude Code not installed"], problems, fixes
    if p["signed_in"] is None:
        find("could not read Claude Code's sign-in state (`claude auth status --json`)",
             "update Claude Code (`claude update`), then rerun this check")
        return [f"{machine}: sign-in state unknown"], problems, fixes
    if not p["signed_in"]:
        find("Claude Code is signed out", f"{login}  (the user signs in with the account they want there)")
        return [f"{machine}: Claude Code signed out"], problems, fixes
    other = False
    if p["method"] and p["method"] != "claude.ai":
        other = True
        find(f"Claude Code is signed in with {p['method']}, not a claude.ai account: claude.ai connectors need one",
             f"only on the user's yes: sign {machine}'s Claude Code into a claude.ai account: {login}")
        head = f"{machine}: signed in with {p['method']}, not claude.ai"
    elif here:
        head = f"{machine}: signed in to claude.ai (the account the other machines are compared with)"
    elif ref.get("signed_in") is not True or not ref.get("account"):
        head = f"{machine}: signed in to claude.ai; account not compared ({HERE_NAME} is not signed in)"
    elif p["account"] == ref["account"]:
        head = f"{machine}: signed in to claude.ai, account same as {HERE_NAME}"
    else:
        other = True
        org = " (same organization)" if p["org"] and p["org"] == ref.get("org") else ""
        head = f"{machine}: signed in to claude.ai, account different{org} from {HERE_NAME}"
        find(f"signed in to a different claude.ai account than {HERE_NAME}: it lists that account's connectors",
             f"only on the user's yes (some keep a separate account per machine on purpose): sign {machine} into "
             f"the account {HERE_NAME} uses: `ssh {machine} claude auth logout`, then `{login}`")
    lines.append(head)
    if p["off"]:
        find(f"claude.ai connectors are turned off there ({CONNECTORS_OFF_VAR}=false)",
             f"unset {CONNECTORS_OFF_VAR} in {machine}'s shell setup, then rerun this check")
    if not p["listed"]:
        find(f"could not list connectors (`claude mcp list` exit {p['rc']})", "rerun this check; if it fails again, "
             "run `claude mcp list` there and read its error")
        return lines, problems, fixes
    acct = f" (on the account {machine} is signed into; or switch accounts as above)" if other else ""
    by_state: Dict[str, List[str]] = {"needs_auth": [], "missing": [], "failed": []}
    for fam, names in fams.items():
        parts = []
        for name in names:
            st = connector_state(p, name)
            parts.append(f"{name} {STATE_WORDS[st]}")
            if st in by_state:
                problems.append(f"{machine}: {name} {STATE_WORDS[st]}")
                if name not in by_state[st]:
                    by_state[st].append(name)
        lines.append(f"{machine} {fam}: " + ", ".join(parts))
    # One step per state for all its connectors: authorization is per account, not per connector or machine.
    if by_state["needs_auth"]:
        fix = (f"in {SETTINGS_STEP}, connect {', '.join(by_state['needs_auth'])}{acct}: once per account, every "
               "machine signed into it then has them; then rerun this check")
        fixes += [fix] if fix not in fixes else []
    if by_state["missing"]:
        fix = (f"in {SETTINGS_STEP}, add and connect {', '.join(by_state['missing'])}{acct} (an organization admin "
               "may have to allow them first); then rerun this check")
        fixes += [fix] if fix not in fixes else []
    if by_state["failed"]:
        fix = (f"rerun this check; if {', '.join(by_state['failed'])} still fail, reconnect them in "
               f"{SETTINGS_STEP}{acct}")
        fixes += [fix] if fix not in fixes else []
    if verified is None and any(connector_state(p, x) == "connected" for names in fams.values() for x in names):
        lines.append(f"{machine}: connected as `claude mcp list` shows it; not verified for headless runs "
                     "(add --verify)")
    note = False
    for fam, v in (verified or {}).items():
        loaded = v.get("loaded") or {}
        idle = [x for x, k in loaded.items() if k == 0]
        if loaded:
            up = [f"{x} {k} tools loaded" for x, k in loaded.items() if k]
            lines.append(f"{machine} {fam} headless (`claude -p`, as tt-project workers run): "
                         + "; ".join(up + ([f"{NOT_LOADED}: {', '.join(idle)}"] if idle else [])))
        if v["ok"]:
            lines.append(f"{machine} {fam} verify: pass ({v['tool']}, {v['items']} items)")
            note = note or bool(idle)
        elif v.get("auth_error"):
            who = next((x for x in fams[fam] if v["tool"].startswith(tool_prefix(x))), fam)
            lines.append(f"{machine} {fam} verify: fail ({v['tool']} answered an auth error after the wait)")
            find(f"{who} needs authentication (an auth error in a headless call)",
                 f"in {SETTINGS_STEP}, reconnect {who}{acct}, then rerun with --verify")
        elif loaded and not v["tool"] and not any(loaded.values()):
            lines.append(f"{machine} {fam} verify: {NOT_LOADED} (no tool loaded after waiting, {v.get('tries') or 1} "
                         f"tries within {VERIFY_CAP_S}s)")
            note = True
        elif loaded and not v["tool"]:
            # Some tools loaded, so no auth fault; none of them made a read-only call (e.g. only a read by id).
            lines.append(f"{machine} {fam} verify: not verified, no read-only call made ({v['error']}); "
                         + ", ".join(x for x, k in loaded.items() if k) + " loaded")
            note = note or bool(idle)
        else:
            lines.append(f"{machine} {fam} verify: fail ({v['error']})")
            find(f"{fam} verify failed ({v['error']})", "rerun with --verify; if it fails again, make the same "
                 "read-only call in an interactive `claude` session there and read its error")
    if note:
        lines.append(f"{machine} {HEADLESS_NOTE}")
    return lines, problems, fixes


def _here(script: str, timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["sh"], 124, "", f"timed out after {timeout:g}s")


def _run(machine: str, script: str, timeout: float) -> subprocess.CompletedProcess:
    return _here(script, timeout) if machine == HERE_NAME else box.ssh_run(machine, ["sh", "-c", script],
                                                                             timeout=timeout)


def inspect(aliases: Sequence[str], fams: Dict[str, List[str]], verify: bool = False,
            here: bool = True) -> "tuple[List[str], List[str], List[str], int]":
    """(lines, problems, fixes, exit code) for this machine (`here`: also reported) and each box."""
    lines: List[str] = []
    problems: List[str] = []
    fixes: List[str] = []
    rc = 0
    ref: Optional[dict] = None
    for m in [HERE_NAME] + [a for a in aliases if a != HERE_NAME]:
        r = _run(m, probe_script(), 300)
        if r.returncode == 124 or (m != HERE_NAME and r.returncode == 255):
            tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
            what = "timed out" if r.returncode == 124 else "unreachable over ssh"
            lines.append(f"{m}: {what} (exit {r.returncode}) {tail[0]}".rstrip())
            problems.append(f"{m}: {what}")
            rc = rc or (255 if r.returncode == 255 else 1)
            if m == HERE_NAME:
                ref = {}
            continue
        p = read_probe(r.stdout)
        verified = None
        if verify and p["signed_in"] and p["listed"]:
            targets = verify_targets(p, fams)
            if targets:
                v = _run(m, verify_script(targets), VERIFY_CAP_S + 60)
                verified = read_verify(v.stdout, targets)
        if m == HERE_NAME:
            ref = p
            if not here:
                continue
        ln, pr, fx = report(m, p, None if m == HERE_NAME else ref, fams, verified)
        lines += ln
        problems += pr
        fixes += [f for f in fx if f not in fixes]
    return lines, problems, fixes, rc or (1 if problems else 0)


def cmd_check(args) -> int:
    try:
        try:
            site = box.load_site(args.site)
        except box.SiteError:
            site = {}
        fams = families(site, args.family or ())
    except ValueError as e:
        print(f"error: {e}")
        return 1
    lines, problems, fixes, rc = inspect(args.alias, fams, args.verify)
    if args.json:
        print(json.dumps({"lines": lines, "problems": problems, "fixes": fixes}))
    else:
        print("\n".join(lines + ["verdict: " + ("problems: " + "; ".join(problems) if problems else "ok")]
                        + (["fix (each one step; an account switch or login only on the user's yes):"]
                           + [f"  {f}" for f in fixes] if fixes else [])))
    return rc


def register(sub) -> None:
    cn = sub.add_parser("connectors", help="the user's claude.ai account connectors here and on each box") \
        .add_subparsers(dest="connectors_cmd", required=True)
    c = cn.add_parser("check", help="read-only: same account as here, each connector connected, needs "
                                    "authentication or missing, each finding with its one step")
    c.add_argument("alias", nargs="*", help="boxes to check besides this machine (ttp machines aliases)")
    c.add_argument("--family", action="append", metavar="FAMILY=A,B",
                   help="add a connector family or extend one (default families: " + ", ".join(FAMILIES) + ")")
    c.add_argument("--verify", action="store_true",
                   help="one real read-only call per connected family on each machine (a small model call each)")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_check)
