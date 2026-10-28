"""setup-box new-box flow: `box.py setup questions|save|plan|hostcheck <alias>`.

- `questions` prints what is still to ask about a box: questions never answered, whose wording or
  options changed since they were answered, or whose earlier answer they depend on changed.
  `--format ask` (default) prints batches for an agent's multiple-choice question tool (at most 4
  questions of 2-4 options each); `--format numbered` prints a numbered prompt for agents without
  one; `--all` asks everything again.
- `save` records answers in the box's site entry (`setup`), so a rerun asks only what is new or
  changed. Nothing is filled in by default: an unanswered question stays pending.
- `plan` turns the saved answers into steps, each an action and the check that proves it. It
  refuses while a question is pending. The actions link to tt-buddy and tt-device-mcp's own docs.
- `hostcheck` is one read-only ssh probe: driver, firmware, hugepages, PCIe link.
- `journalcheck` is one read-only ssh probe: whether the login reads the system journal (reboot causes);
  `--save` records `journal=readable` so that question is skipped.
- `mount` makes the saved shared network mount persistent (sudo -n, else printed for the user);
  `mountcheck` verifies it now and, with `--after-reboot`, once the box has rebooted; `--rebooted` is the
  change entry's until-probe (`box_storage`).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re
import shlex
import sys
import tempfile
from typing import Dict, List, Optional

import box
import box_account
import box_agents
import box_storage

KNOWN_KEYS = {"setup"}
HOST_CHECKS = ("driver", "firmware", "hugepages", "pcie")
AGENT_CLIS = {"claude": "claude --version", "codex": "codex --version", "cursor": "cursor-agent --version"}
TT_PCI_VENDOR = "0x1e52"
JOURNAL_GROUPS = {"systemd-journal", "adm", "wheel"}
DEFAULT_GUARD_GB = 150  # the harness's own disk.min_free_gb default


def _repo(v: str) -> bool:
    parts = v.split("/")
    return len(parts) == 2 and all(p and not p.startswith(".") and all(c.isalnum() or c in "-_." for c in p)
                                   for p in parts)


def _word(v: str) -> bool:
    return bool(v) and not any(c.isspace() for c in v) and not v.startswith("-")


def _names(v: str) -> bool:
    return all(_word(n) for n in v.split(","))


def _pubs(v: str) -> bool:
    return all(n == "here" or _word(n) and n.endswith(".pub") for n in v.split(","))


def _abs_paths(v: str) -> bool:
    return all(_word(n) and n.startswith("/") for n in v.split(","))


def _gb(v: str) -> bool:
    try:
        return float(v) >= 0
    except ValueError:
        return False


# Each question: id, header (<= 12 chars), text, options [(value, label, description)], suggest
# (offered first and marked, never filled in), multi, other (a validator for a typed answer), when
# (the earlier answers it needs) and parent (whose change makes it ask again).
QUESTIONS: List[dict] = [
    dict(id="account.user", header="Login user", suggest="same", other=box_account.valid_login,
         text="Your login on {alias}? (another name: type it)", options=[
             ("same", "Same as here", "{user}, the name you have on your other machines")]),
    dict(id="account.admin", header="First login", other=_word,
         text="If your own login does not reach {alias} yet: the ssh alias of the admin or site account "
              "access came through? (type it)", options=[
             ("none", "My login works", "ssh as your own user already works; no account is created")]),
    dict(id="account.ids", header="uid/gid", parent="account.admin", when={"account.admin": "!none"},
         suggest="here", other=_word,
         text="Where do your uid and gid come from, so files on shared storage keep their owner? "
              "(another machine's ssh alias, or a path on {alias} you own: type it)", options=[
             ("here", "This machine", "`id` here")]),
    dict(id="account.migrate", header="Move work", parent="account.admin", when={"account.admin": "!none"},
         other=_abs_paths,
         text="Work already done in the admin account on {alias} to move into your home? "
              "(type absolute paths, comma-separated)", options=[
             ("none", "Nothing to move", "the admin account holds none of your work")]),
    dict(id="account.keys", header="ssh keys", suggest="here", other=_pubs,
         text="Public keys for your authorized_keys on {alias}? (others: type .pub paths, comma-separated; "
              "here = this machine's)", options=[
             ("here", "This machine's", "every ~/.ssh/*.pub here, so the project host gets in")]),
    dict(id="account.outbound", header="Outbound", text="Your own ssh keys for git over ssh from {alias}?",
         options=[
             ("none", "None to restore", "git over https, or a key set up later"),
             ("restore", "Restore mine", "you copy your key files into ~/.ssh on {alias}; the plan checks modes")]),
    dict(id="broker", header="Broker", text="Device broker on {alias}?", suggest="install", options=[
        ("install", "Install tt-device-mcp", "through its documented installer; device jobs queue through it"),
        ("none", "No broker", "each device command takes `ttp lock {lock} -- <cmd>` instead")]),
    dict(id="broker.ref", header="Broker ref", parent="broker", when={"broker": "install"},
         text="Which tt-device-mcp release or branch?", suggest="latest-release", other=_word, options=[
             ("latest-release", "Latest release", "the newest tagged release"),
             ("main", "main branch", "the default branch; type another tag or branch under Other")]),
    dict(id="host_checks", header="Host checks", multi=True, suggest=list(HOST_CHECKS),
         text="Read-only host checks to run on {alias}? (none: type none)", options=[
             ("driver", "Driver", "the tenstorrent kernel module and its device nodes"),
             ("firmware", "Firmware", "firmware versions the driver reports"),
             ("hugepages", "Hugepages", "1 GB hugepages reserved"),
             ("pcie", "PCIe link", "each card's link speed and width against its maximum")]),
    dict(id="journal", header="Journal", suggest="add",
         text="Let your login on {alias} read the system journal, so tt-project's reboot notices name what "
              "rebooted it? (`setup journalcheck --save` skips this when it already can)", options=[
             ("add", "Add to group", "add your login to the systemd-journal group (sudo, once); it applies "
                                     "from your next login, and to running user services from the next reboot"),
             ("no", "Leave it", "reboot notices then say the journal is not readable"),
             ("readable", "Already readable", "your login on {alias} reads the system journal already")]),
    dict(id="storage", header="Shared mount", other=box_storage.valid_mountpoint,
         text="Should {alias} mount shared network storage (caches, backups) at every boot? A mount made by "
              "hand is gone after the next reboot. (yes: type its mount point, an absolute path)", options=[
             ("none", "No shared mount", "nothing is mounted or changed")]),
    dict(id="storage.source", header="Mount source", parent="storage", when={"storage": "!none"},
         suggest="live", other=box_storage.valid_spec,
         text="What to mount there, as the filesystem's client names it? (type it, e.g. <server>:/<export>)",
         options=[("live", "As mounted now", "the source mounted at the mount point on {alias} now")]),
    dict(id="storage.fstype", header="Mount type", parent="storage", when={"storage": "!none"},
         suggest="live", other=box_storage.valid_fstype,
         text="Its filesystem type? (type it, e.g. nfs4)", options=[
             ("live", "As mounted now", "the type mounted at the mount point on {alias} now")]),
    dict(id="storage.options", header="Mount opts", parent="storage", when={"storage": "!none"},
         suggest="defaults", other=box_storage.valid_spec,
         text="Mount options? _netdev (and nofail in fstab) are always added. (others: type them, comma-separated)",
         options=[("defaults", "Defaults", "the filesystem's default options")]),
    dict(id="storage.method", header="Persist how", parent="storage", when={"storage": "!none"},
         suggest="fstab", text="How should the mount come back after a reboot?", options=[
             ("fstab", "fstab line", "an /etc/fstab line with _netdev,nofail: waits for the network, never "
                                     "blocks boot"),
             ("systemd", "systemd unit", "a .mount unit ordered after the network and the filesystem's client "
                                         "agent service, for filesystems that need one")]),
    dict(id="storage.agent", header="Client agent", parent="storage.method", when={"storage.method": "systemd"},
         other=box_storage.valid_unit,
         text="The filesystem's client agent service on {alias}, if it has one? (type its unit name)", options=[
             ("none", "No client agent", "the unit waits for the network only")]),
    dict(id="workspace", header="Workspace", suggest="tenstorrent/tt-metal", other=_repo,
         text="A repo checkout and build under the workdir root? (another repo: type owner/name)", options=[
             ("tenstorrent/tt-metal", "tt-metal", "clone and build tenstorrent/tt-metal"),
             ("none", "No workspace", "tasks bring their own checkout")]),
    dict(id="workspace.branch", header="Branch", parent="workspace", when={"workspace": "!none"},
         text="Which branch for the workspace?", suggest="main", other=_word, options=[
             ("main", "main", "the default branch"),
             ("latest-release", "Latest release tag", "the newest tagged release; another branch: type it")]),
    dict(id="workspace.ccache", header="ccache", parent="workspace", when={"workspace": "!none"},
         text="Build with ccache?", suggest="yes", options=[
             ("yes", "Yes", "faster rebuilds; needs ccache installed on the box"),
             ("no", "No", "plain builds")]),
    dict(id="python_env", header="Python env", text="Python env for runs on {alias}?",
         suggest="workspace", options=[
             ("pip-ttnn", "pip ttnn", "a virtualenv with the released ttnn wheel"),
             ("workspace", "Workspace env", "the workspace's own env, built with it"),
             ("none", "None", "tasks set up their own env")]),
    dict(id="agent_clis", header="Agent CLIs", multi=True, suggest=["claude", "codex"],
         text="Agent CLIs on {alias}, for your editor and the command line? (official installers, as your user; "
              "on PATH for ssh commands and login shells)", options=[
             ("claude", "Claude Code", "its official installer, checked with `claude --version`"),
             ("codex", "Codex", "its official installer, checked with `codex --version`"),
             ("cursor", "Cursor agent", "checked with `cursor-agent --version`"),
             ("none", "None", "agents drive the box over ssh from the project box")]),
    dict(id="agents.login", header="CLI logins", parent="agent_clis", when={"agent_clis": "!none"}, suggest="yes",
         text="Log the CLIs in on {alias} now? Each shows a sign-in link and a code you enter in your browser; "
              "the login stays on {alias}.", options=[
             ("yes", "Log in now", "you only enter the code it shows; checked with `claude auth status` and "
                                   "`codex login status`"),
             ("no", "Later", "the box check reports them as not logged in")]),
    dict(id="editor", header="Editor", text="Do you open {alias} in an editor over Remote-SSH?", options=[
        ("cursor", "Cursor", "Cursor's Remote-SSH"),
        ("vscode", "VS Code", "VS Code's Remote-SSH"),
        ("both", "Both", "Cursor and VS Code"),
        ("none", "No editor", "the command line only")]),
    dict(id="agents.extensions", header="Extensions", parent="editor", when={"editor": "!none"}, suggest="yes",
         text="Install the Claude Code and Codex extensions on every editor server build on {alias}? Your editor's "
              "own login does not carry over: they log in on {alias}.", options=[
             ("yes", "Install them", f"{', '.join(box_agents.EXTENSIONS)}, checked with --list-extensions"),
             ("no", "No", "the editor on {alias} has no Claude Code or Codex")]),
    dict(id="agents.defaults", header="New builds", parent="editor", when={"editor": "!none"}, suggest="yes",
         text="An editor update brings a new server build without them. Add both to remote.SSH.defaultExtensions "
              "in your editor's user settings on this machine?", options=[
             ("yes", "Add them", "merged into the list, existing entries kept, the file backed up first"),
             ("no", "No", "the box check finds a new build without them and installs them")]),
    dict(id="projects", header="Projects", suggest="all", other=_names,
         text="Which of your projects may use {alias}? (some: type their names, comma-separated)", options=[
             ("all", "All my projects", "every project whose charter names it"),
             ("none", "None yet", "add it to a project's charter later")]),
    dict(id="recovery.owner", header="Recovery", other=_word,
         text="Which project recovers {alias} when it stays down or held? (type the project name)", options=[
             ("none", "No owner", "you are asked each time")]),
    dict(id="recovery.fallback", header="Fallback", parent="recovery.owner", when={"recovery.owner": "!none"},
         other=_word, text="Which project takes over when the owner cannot act? (type the project name)",
         options=[("none", "No fallback", "you are asked")]),
    dict(id="lock", header="Device lock", suggest="shared", text="One device lock for {alias}, or one per project?",
         options=[
             ("shared", "Shared", "all your projects queue on one `{lock}` lock"),
             ("per-project", "Per project", "each project locks on its own; only for chips split between them")]),
    dict(id="disk_guard", header="Disk guard", suggest=str(DEFAULT_GUARD_GB), other=_gb,
         text="Free-space guard in GB under the workdir root? (another value: type it)", options=[
             (str(DEFAULT_GUARD_GB), f"{DEFAULT_GUARD_GB} GB", "the harness default; enough for a tt-metal build"),
             ("0", "Off", "no guard")]),
]
BY_ID = {q["id"]: q for q in QUESTIONS}


def qhash(q: dict) -> str:
    """Changes when a question's wording, options or condition change."""
    doc = {k: q.get(k) for k in ("text", "options", "multi", "when")}
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:12]


def applies(q: dict, answers: dict) -> bool:
    for k, want in (q.get("when") or {}).items():
        got = answers.get(k)
        if isinstance(got, list):   # a multi answer: [] is none
            got = ",".join(got) or "none"
        if got is None:
            return False
        if want.startswith("!") and got == want[1:] or not want.startswith("!") and got != want:
            return False
    return True


def pending(saved: dict, ask_all: bool = False) -> List[dict]:
    """The questions to ask now: those that apply and are new, changed or never answered."""
    answers, asked = saved.get("answers") or {}, saved.get("asked") or {}
    out = []
    for q in QUESTIONS:
        if not applies(q, answers):
            continue
        if ask_all or q["id"] not in answers or asked.get(q["id"]) != qhash(q):
            out.append(q)
    return out


def _fmt(s: str, alias: str, lock: str) -> str:
    return s.replace("{alias}", alias).replace("{lock}", lock).replace("{user}", box_account.local_user())


def render_ask(qs: List[dict], alias: str, lock: str, saved: dict) -> List[dict]:
    """Batches for a multiple-choice question tool; its own free-text choice covers typed answers."""
    answers = saved.get("answers") or {}
    batches, cur = [], []
    for q in qs:
        sug = q.get("suggest")
        prev = answers.get(q["id"])
        opts = []
        for v, label, desc in q["options"]:
            mark = []
            if v == sug or isinstance(sug, list) and v in sug:
                mark.append("suggested")
            if v == prev or isinstance(prev, list) and v in prev:
                mark.append("saved")
            opts.append({"label": label + (f" ({', '.join(mark)})" if mark else ""),
                         "description": _fmt(desc, alias, lock), "value": v})
        if len(opts) < 2:  # the tool needs two; the typed answer is the other path
            opts.append({"label": "Type it", "description": "choose the free-text option and type it", "value": None})
        cur.append({"id": q["id"], "header": q["header"][:12], "question": _fmt(q["text"], alias, lock),
                    "multiSelect": bool(q.get("multi")), "options": opts[:4]})
        if len(cur) == 4:
            batches.append(cur)
            cur = []
    if cur:
        batches.append(cur)
    return batches


def render_numbered(qs: List[dict], alias: str, lock: str, saved: dict) -> str:
    answers = saved.get("answers") or {}
    lines = [f"Setting up {alias}. Reply with one line per question, e.g. `1 b` or `3 a,c`; "
             "no reply leaves a question open (nothing is assumed)."]
    for n, q in enumerate(qs, 1):
        lines.append(f"{n}. {_fmt(q['text'], alias, lock)}" + (" (pick any)" if q.get("multi") else ""))
        for i, (v, label, desc) in enumerate(q["options"]):
            lines.append(f"   {chr(97 + i)}) {label}: {_fmt(desc, alias, lock)}")
        sug = q.get("suggest")
        extra = [f"suggested: {', '.join(sug) if isinstance(sug, list) else sug}"] if sug else []
        if q["id"] in answers:
            prev = answers[q["id"]]
            extra.append(f"saved: {', '.join(prev) if isinstance(prev, list) else prev}")
        if q.get("other"):
            extra.append("or type a value")
        if extra:
            lines.append(f"   ({'; '.join(extra)})")
    return "\n".join(lines)


def parse_value(q: dict, raw) -> object:
    """A validated answer for `q`; ValueError on a bad one."""
    values = [v for v, _, _ in q["options"]]
    labels = {label.lower(): v for v, label, _ in q["options"]}

    def one(s) -> str:
        s = re.sub(r"\s*\((suggested|saved)(, saved)?\)$", "", str(s).strip())  # a label as render_ask marks it
        return labels.get(s.lower(), s)
    if q.get("multi"):
        items = raw if isinstance(raw, list) else str(raw).split(",")
        items = [one(s) for s in items if str(s).strip()]
        bad = [s for s in items if s not in values and s != "none"]
        if bad or not items:
            raise ValueError(f"{q['id']}: pick from {', '.join(values)}, not {', '.join(bad) or 'nothing'}")
        if "none" in items:
            if len(items) > 1:
                raise ValueError(f"{q['id']}: none goes alone")
            return []
        return sorted(set(items), key=values.index)
    if raw is None or not str(raw).strip():
        raise ValueError(f"{q['id']}: an answer is needed, not an empty one")
    s = one(raw)
    if s in values:
        return s
    if q.get("other") and q["other"](s):
        return s
    raise ValueError(f"{q['id']}: {s!r} is not one of {', '.join(values)}"
                     + (" or a valid typed value" if q.get("other") else ""))


def merge(saved: dict, new: Dict[str, object]) -> dict:
    """`saved` with `new` answers recorded; a changed answer drops the answers that depend on it."""
    answers = dict(saved.get("answers") or {})
    asked = dict(saved.get("asked") or {})
    rest = {k: v for k, v in saved.items() if k not in ("answers", "asked", "saved")}
    for qid, raw in new.items():
        q = BY_ID.get(qid)
        if q is None:
            raise ValueError(f"unknown question {qid!r} (known: {', '.join(BY_ID)})")
        val = parse_value(q, raw)
        if qid in answers and answers[qid] != val:
            if qid.split(".")[0] == "storage":
                rest.pop("storage_verify", None)  # a first check of another mount proves nothing
            for dep in _dependents(qid):
                if dep not in new:
                    answers.pop(dep, None)
                    asked.pop(dep, None)
        answers[qid] = val
        asked[qid] = qhash(q)
    return {**rest, "answers": answers, "asked": asked,
            "saved": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}


def _dependents(qid: str) -> List[str]:
    out = [q["id"] for q in QUESTIONS if q.get("parent") == qid]
    return out + [d for c in out for d in _dependents(c)]


def _sh(path: str) -> str:
    """`path` as a remote shell word: a plain path (also ~/...) as is, so ~ expands on the box."""
    return path if all(c.isalnum() or c in "/._-~" for c in path) else shlex.quote(path)


def lock_name(alias: str) -> str:
    """The device lock; `lock` only says whether your projects share its slots (`ttp machines add --shared`)."""
    return f"{alias}-device"


def login_user(answers: dict) -> str:
    u = answers.get("account.user", "same")
    return box_account.local_user() if u == "same" else u


def account_steps(alias: str, entry: dict, answers: dict, box_py: str, step) -> None:
    """The user's own account first: everything after it is set up in that account (`account.md`)."""
    user = login_user(answers)
    admin = answers.get("account.admin", "none")
    keys = [k for k in (answers.get("account.keys") or "here").split(",") if k]
    pub = "" if keys == ["here"] else "".join(f"--pubkey {_sh(k)} " for k in keys)
    if admin != "none":
        ids = answers.get("account.ids", "here")
        step("account", f"`{box_py} account create {alias} --via {admin} --user {user} --ids-from {_sh(ids)} "
                        f"{pub}--run` after reading its dry run: the same uid/gid as on {ids}, a home, a login "
                        "shell, sudo only if the admin login has it (a drop-in checked with `visudo -cf`), the docker "
                        "group if there is one, authorized_keys and linger; no other account is changed",
             f"ssh {alias} 'id; loginctl show-user {user} -p Linger'  (expect: {user} with the uid/gid "
             f"`{box_py} account ids {_sh(ids)}` prints, Linger=yes)")
        moves = answers.get("account.migrate", "none")
        if moves != "none":
            paths = moves.split(",")
            step("account.migrate", f"`{box_py} account migrate {alias} --via {admin} --user {user} "
                                    + " ".join(f"--path {_sh(p)}" for p in paths) + " --run` after its dry run: "
                                    "mv and chown, never a delete; rebuild each venv and build tree it lists; then "
                                    "remove from the admin account only what setup-box itself put there",
                 f"{box_py} account migrate {alias} --via {admin} --user {user} "
                 + " ".join(f"--path {_sh(p)}" for p in paths) + "  (dry run; expect: each path `moved`, none `to move`)")
    else:
        step("account", f"your own login works: `{box_py} account keys {alias} {pub}--run` adds the keys; "
                        f"`ssh {alias} loginctl enable-linger` (or through the box's admin, if refused)",
             f"ssh {alias} 'id -un; loginctl show-user {user} -p Linger'  (expect: {user}, Linger=yes)")
    if answers.get("account.outbound") == "restore":
        step("account.outbound", f"the user copies their own key files into ~/.ssh on {alias} (agents never "
                                 "handle private keys)",
             f"ssh {alias} 'stat -c \"%a %n\" ~/.ssh/id_*'  (expect: 600 for each private key)")
    step("account.entry", f"`{box_py} account entry {alias} --user {user}"
                          + (f" --admin {admin}" if admin != "none" else "") + "`: workdir_root under the "
                          f"home; ssh config `Host {alias}` with `User {user}`"
                          + (f", and `Host {admin}` for the admin login" if admin != "none" else ""),
         f"{box_py} site check; ssh {alias} id -un  (expect: ok; {user})")


def agent_steps(alias: str, answers: dict, box_py: str, step) -> None:
    """Agent CLIs, their logins and the editor extensions on the box, all run by `box.py agents` (`agents.md`)."""
    chosen = answers.get("agent_clis") or []
    clis = [c for c in chosen if c in box_agents.CLIS]
    if clis:
        step("agent_clis", f"`{box_py} agents install {alias} --cli {','.join(clis)} --run`: the official installers "
                           "as your user (no root), skipped when `--version` already works; ~/.local/bin on PATH for "
                           f"ssh commands and login shells; the {box_agents.MCP} MCP server in each CLI when its URL "
                           "is configured",
             f"{box_py} agents check {alias} --cli {','.join(clis)}  (expect: each CLI with its version, on PATH)")
    if "cursor" in chosen:
        step("agent_clis", f"install the Cursor agent CLI on {alias} for the user, per its official instructions",
             f"ssh {alias} {AGENT_CLIS['cursor']}")
    if answers.get("agents.login") == "yes":
        for c in clis:
            step("agents.login", f"`{box_py} agents login {alias} {c}`: tell the user its sign-in link and code word "
                                 "for word; they enter the code in their browser; tokens stay on the box",
                 f"{box_py} agents login {alias} {c} --status  (exit 0 logged in)")
        for c in clis:
            step("agents.login", f"`{box_py} agents login {alias} {c} --mcp {box_agents.MCP}` once its server is set "
                                 "up: tell the user its sign-in link; pass back the address the browser ends on",
                 f"{box_py} agents login {alias} {c} --mcp {box_agents.MCP} --status  (exit 0 signed in)")
    if "claude" in clis and answers.get("agents.login") == "yes":
        step("connectors", f"`{box_py} connectors check {alias} --verify`: the box's Claude Code on the same claude.ai "
                           "account as this machine (a switch only on the user's yes), its Atlassian and Google "
                           "connectors connected, one read-only call per family (`connectors.md`)",
             f"{box_py} connectors check {alias} --verify  (expect: account same; each connector connected; "
             "each verify pass)")
    if answers.get("editor", "none") != "none" and answers.get("agents.extensions") == "yes":
        step("agents.extensions", f"`{box_py} agents install {alias} --cli none --extensions --run`: "
                                  f"{' and '.join(box_agents.EXTENSIONS)} on every Cursor and VS Code server build on "
                                  f"{alias} (an editor's first Remote-SSH connect makes one: connect first)",
             f"{box_py} agents check {alias}  (expect: each editor build `extensions ok`)")
    if answers.get("editor", "none") != "none" and answers.get("agents.defaults") == "yes":
        step("agents.defaults", f"on this machine, where your editor runs: `{box_py} agents editor-defaults --run`: "
                                "remote.SSH.defaultExtensions in Cursor's and VS Code's user settings, merged and "
                                "backed up, so a new server build on any box gets both",
             f"{box_py} agents editor-defaults  (expect: already has {', '.join(box_agents.EXTENSIONS)})")


def plan(alias: str, entry: dict, answers: dict, plugin_root: str = "<plugin-root>") -> List[dict]:
    """Ordered steps, each {"id", "action", "check"}; every answer becomes an action and its check."""
    box_py = f"{plugin_root}/skills/setup-box/scripts/box.py"
    root = entry.get("workdir_root", "<workdir_root>")
    lock = lock_name(alias)
    steps: List[dict] = []

    def step(qid, action, check):
        steps.append({"id": qid, "action": action, "check": check})

    if "account.user" in answers:
        account_steps(alias, entry, answers, box_py, step)
    if answers.get("broker") == "install":
        ref = answers.get("broker.ref", "latest-release")
        step("broker", f"install tt-device-mcp ({ref}) on {alias} through the tenstorrent/tt-device-mcp "
                       "repository's documented installer; never hand-edit an installed broker",
             f"{box_py} bare check {alias}  (expect: broker: tt-device-mcp present, tt-device-broker active)")
    elif answers.get("broker") == "none":
        step("broker", f"no broker: every command that touches the device runs as "
                       f"`ttp lock {lock} -- <command>`, held only for that command",
             f"ttp lock --probe {lock}  (exit 0 free, 75 busy or paused)")
    checks = answers.get("host_checks") or []
    if checks:
        step("host_checks", "nothing to change: the checks are read-only; fix what they report with tt-buddy's "
                            "`recipes/developer-setup.md`",
             f"{box_py} setup hostcheck {alias} --only {','.join(checks)}")
    if answers.get("journal") == "add":
        user = login_user(answers)
        step("journal", f"on {alias}: `sudo usermod -aG systemd-journal {user}` (sudo -n; if it needs a password, "
                        "the user runs it once); it applies from the next login, and to running user services "
                        "(the tt-project daemon) from the next reboot. Never reboot a shared box for it",
             f"{box_py} setup journalcheck {alias}  (expect: in group systemd-journal; readable from a new login)")
    mnt = box_storage.spec(answers)
    if mnt:
        how = ("a systemd .mount unit" + (f" after {box_storage.agent_unit(mnt['agent'])}"
                                          if box_storage.agent_unit(mnt["agent"]) else "")
               if mnt["method"] == "systemd" else "an /etc/fstab line with _netdev,nofail")
        step("storage", f"`{box_py} setup mount {alias} --run` after reading its dry run: {mnt['where']} mounted at "
                        f"every boot through {how}, as root with sudo -n (without passwordless sudo it prints the "
                        "root script: a step for the user to run with their privileges); then record the change "
                        "with the `ttp machines change add` line it prints",
             f"{box_py} setup mountcheck {alias}  (expect: mounted, read ok, persistent; it records the boot id)")
        step("storage.reboot", "nothing now, and never reboot a shared box for it: the change entry's "
                               "--until-probe (`mountcheck --rebooted`) turns it due once the box next reboots; "
                               "then run this check: on a pass close the entry and keep the mount",
             f"{box_py} setup mountcheck {alias} --after-reboot  (exit 0 verified after a reboot, 1 no reboot "
             "yet, 2 failed after a reboot, 255 ssh failed)")
    ws = answers.get("workspace")
    if ws and ws != "none":
        name = ws.split("/")[1]
        path = f"{root.rstrip('/')}/{name}"
        branch = answers.get("workspace.branch", "main")
        cc = " with ccache (`build_metal.sh --help` names its flag)" if answers.get("workspace.ccache") == "yes" else ""
        step("workspace", f"on {alias}: clone {ws} at {branch} into {path}, "
                          "`git submodule update --init --recursive`, and build in place" + cc +
                          " as tt-buddy's `recipes/workspace.md` and `recipes/tt-metal/build.md` say",
             f"ssh {alias} git -C {_sh(path)} rev-parse --abbrev-ref HEAD  (expect: {branch}, "
             "or a detached HEAD at a tag)")
    env = answers.get("python_env")
    if env == "pip-ttnn":
        venv = f"{root.rstrip('/')}/ttnn-venv"
        step("python_env", f"on {alias}: a virtualenv at {venv} with the ttnn wheel, as tt-buddy's "
                           "`recipes/developer-setup.md` says",
             f"ssh {alias} {_sh(venv)}/bin/python -m pip show ttnn")
    elif env == "workspace":
        if not ws or ws == "none":
            step("python_env", "WARNING: python_env is workspace but there is no workspace; ask python_env "
                               "again (pip-ttnn or none) or answer workspace first",
                 "none: no check until python_env or workspace is changed")
        else:
            where = f"{root.rstrip('/')}/{ws.split('/')[1]}"
            step("python_env", "the workspace's own env, built with it (tt-buddy's `recipes/tt-metal/env.md`); "
                               "a broker job needs it too (the `tt-buddy:run` skill's `skills/run/mcp.md`)",
                 f"ssh {alias} test -x {_sh(where)}/python_env/bin/python")
    agent_steps(alias, answers, box_py, step)
    projects = answers.get("projects")
    if projects and projects != "none":
        who = "each of your projects" if projects == "all" else f"projects {projects}"
        step("projects", f"name {alias} in the Resources of {who}' charter (a charter edit, by the user or "
                         "its coordinator)", f"ttp machines status {alias}  (lists the projects using it)")
    owner = answers.get("recovery.owner")
    if owner:
        fb = answers.get("recovery.fallback", "none")
        o = "" if owner == "none" else owner
        f = "" if fb == "none" or not o else fb
        step("recovery", f"ttp machines add {alias} --owner {shlex.quote(o)} --fallback {shlex.quote(f)}",
             f"ttp machines status {alias}  (expect: "
             + (f"recovery owner {o}" + (f", fallback {f}" if f else "") if o else "no recovery owner") + ")")
    if answers.get("lock") == "shared":
        step("lock", f"ttp machines add {alias} --shared {lock}",
             f"ttp machines list --json  (expect: {alias} shared [{lock}])")
    elif answers.get("lock") == "per-project":
        step("lock", f"ttp machines add {alias} --unshared", f"ttp machines list --json  (expect: {alias} not shared)")
    if "disk_guard" in answers:
        step("disk_guard", f"ttp machines add {alias} --min-free-gb {answers['disk_guard']}",
             f"{box_py} bare check {alias}  (expect: guard {float(answers['disk_guard']):g} GB)")
    return steps


def save_site(path: pathlib.Path, site: dict) -> None:
    """Write the site file atomically, owner-only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".boxes.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(site, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def hostcheck_script() -> str:
    """Read-only shell run on the box: sysfs, /proc and /dev listings only; it opens no device."""
    return (
        'echo "driver=$(cat /sys/module/tenstorrent/version 2>/dev/null)"; '
        'echo "dev_nodes=$(ls /dev/tenstorrent 2>/dev/null | wc -l)"; '
        'for f in /sys/class/tenstorrent/*/tt_fw_bundle_ver; do [ -r "$f" ] && '
        'echo "fw=$(basename "$(dirname "$f")") $(cat "$f")"; done; '
        'echo "hp1g=$(cat /sys/kernel/mm/hugepages/hugepages-1048576kB/nr_hugepages 2>/dev/null)"; '
        'for d in /sys/bus/pci/devices/*; do '
        f'[ "$(cat "$d/vendor" 2>/dev/null)" = {TT_PCI_VENDOR} ] || continue; '
        'echo "pcie=$(basename "$d") $(cat "$d/current_link_speed" 2>/dev/null | tr " " _)'
        ' $(cat "$d/current_link_width" 2>/dev/null) $(cat "$d/max_link_speed" 2>/dev/null | tr " " _)'
        ' $(cat "$d/max_link_width" 2>/dev/null)"; done; true'
    )


def read_hostcheck(out: str, only=HOST_CHECKS) -> "tuple[List[str], List[str]]":
    """(report lines, problems) from the probe's key=value lines."""
    rows = [ln.split("=", 1) for ln in out.splitlines() if "=" in ln]
    kv = {k: v for k, v in rows}
    lines: List[str] = []
    problems: List[str] = []
    try:
        nodes = int(kv.get("dev_nodes", "0") or 0)
    except ValueError:
        nodes = 0
    if "driver" in only:
        ver = kv.get("driver", "").strip()
        if not ver:
            problems.append("driver")
            lines.append("driver: the tenstorrent module is not loaded: install it per tt-buddy's "
                         "`recipes/developer-setup.md`")
        elif not nodes:
            problems.append("driver")
            lines.append(f"driver: tenstorrent {ver} loaded but no /dev/tenstorrent nodes")
        else:
            lines.append(f"driver: tenstorrent {ver}, {nodes} device node(s)")
    if "firmware" in only:
        fw = [v for k, v in rows if k == "fw"]
        lines.append("firmware: " + ("; ".join(fw) if fw else
                     "not reported by the driver (older driver): check it per tt-buddy's "
                     "`recipes/developer-setup.md`"))
    if "hugepages" in only:
        hp = kv.get("hp1g", "").strip()
        lines.append(f"hugepages: {hp or 0} x 1 GB reserved" +
                     ("" if hp and hp != "0" else "; whether the cards need them: tt-buddy's "
                                                  "`recipes/developer-setup.md`"))
    if "pcie" in only:
        links = [v.split() for k, v in rows if k == "pcie"]
        if not links:
            problems.append("pcie")
            lines.append("pcie: no Tenstorrent card on the PCI bus")
        for ln in links:
            dev, cur, width, top, top_w = (ln + [""] * 5)[:5]
            low = cur != top or width != top_w
            lines.append(f"pcie: {dev} {cur.replace('_', ' ')} x{width}"
                         + (f", below its maximum {top.replace('_', ' ')} x{top_w}: check the slot and riser"
                            if low else ""))
    return lines, problems


def _entry(args) -> "tuple[dict, dict]":
    site = box.load_site(args.site)
    return site, box.box_entry(site, args.alias)


def cmd_questions(args) -> int:
    try:
        _, entry = _entry(args)
    except box.SiteError as e:
        print(f"error: {e}; write the box's entry first (site.md)")
        return 1
    saved = entry.get("setup") or {}
    qs = pending(saved, args.all)
    lock = lock_name(args.alias)
    if not qs:
        print(f"nothing to ask: every answer for {args.alias} is saved; `box.py setup plan {args.alias}` next")
        return 0
    if args.format == "numbered":
        print(render_numbered(qs, args.alias, lock, saved))
    else:
        print(json.dumps({"alias": args.alias, "batches": render_ask(qs, args.alias, lock, saved)}, indent=2))
    return 0


def cmd_save(args) -> int:
    new: Dict[str, object] = {}
    try:
        if args.json:
            doc = json.loads(args.json)
            if not isinstance(doc, dict):
                raise ValueError("--json must be an object of question id to answer")
            new.update(doc)
        for kv in args.set or []:
            if "=" not in kv:
                raise ValueError(f"--set {kv!r}: use id=value")
            k, v = kv.split("=", 1)
            new[k.strip()] = v
        site, entry = _entry(args)
        entry["setup"] = merge(entry.get("setup") or {}, new)
        save_site(box.site_path(args.site), site)
    except (ValueError, box.SiteError) as e:
        print(f"error: {e}")
        return 1
    left = pending(entry["setup"])
    print(f"saved {len(new)} answer(s) for {args.alias}; "
          + (f"still to ask: {', '.join(q['id'] for q in left)}" if left else "nothing left to ask"))
    return 0


def cmd_plan(args) -> int:
    try:
        _, entry = _entry(args)
    except box.SiteError as e:
        print(f"error: {e}")
        return 1
    saved = entry.get("setup") or {}
    left = pending(saved)
    if left:
        print(f"not yet: ask first ({', '.join(q['id'] for q in left)}); `box.py setup questions {args.alias}`")
        return 1
    steps = plan(args.alias, entry, saved.get("answers") or {}, args.plugin_root)
    if args.json:
        print(json.dumps(steps))
    else:
        for n, s in enumerate(steps, 1):
            print(f"{n}. [{s['id']}] {s['action']}\n   check: {s['check']}")
    return 0


def journalcheck_script() -> str:
    """Read-only: the login's uid and groups, and how many recent system-journal entries are not its own."""
    return ("echo uid=$(id -u); echo groups=$(id -nG); "
            "echo foreign=$(journalctl --system -n 200 -o json --output-fields=_UID --no-pager 2>/dev/null "
            "| grep -vc \"\\\"_UID\\\":\\\"$(id -u)\\\"\")")


def read_journalcheck(out: str) -> "tuple[bool, str]":
    """(readable, why) from the probe's key=value lines: root, a journal-reading group (systemd-journal,
    adm, wheel), or system entries that are not the login's own."""
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    groups = set(kv.get("groups", "").split()) & JOURNAL_GROUPS
    try:
        foreign = int(kv.get("foreign", "0").strip() or 0)
    except ValueError:
        foreign = 0
    if kv.get("uid", "").strip() == "0":
        return True, "root"
    if groups:
        return True, f"in group {', '.join(sorted(groups))}"
    if foreign:
        return True, f"{foreign} recent system entries visible"
    return False, "not in systemd-journal, adm or wheel, and no system entries visible"


def cmd_journalcheck(args) -> int:
    """Exit 0 readable, 1 not, 255 ssh failed. --save records journal=readable so the question is skipped;
    a box that cannot read it keeps the question, which only the user answers."""
    r = box.ssh_run(args.alias, journalcheck_script(), timeout=30)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
        print(f"ssh: connection to {args.alias} failed (exit {r.returncode}) {tail[0]}".rstrip())
        return 255 if r.returncode == 255 else 1
    ok, why = read_journalcheck(r.stdout)
    print(f"journal on {args.alias}: {'readable' if ok else 'not readable'} ({why})")
    if ok and args.save:
        try:
            site, entry = _entry(args)
            if (entry.get("setup") or {}).get("answers", {}).get("journal") is None:
                entry["setup"] = merge(entry.get("setup") or {}, {"journal": "readable"})
                save_site(box.site_path(args.site), site)
                print("saved journal=readable: the question is skipped")
        except (ValueError, box.SiteError) as e:
            print(f"error: {e}")
            return 1
    return 0 if ok else 1


def cmd_hostcheck(args) -> int:
    only = tuple(s for s in (args.only or ",".join(HOST_CHECKS)).split(",") if s)
    bad = [s for s in only if s not in HOST_CHECKS]
    if bad:
        print(f"error: unknown check {', '.join(bad)} (known: {', '.join(HOST_CHECKS)})")
        return 1
    r = box.ssh_run(args.alias, hostcheck_script(), timeout=60)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
        print(f"ssh: connection to {args.alias} failed (exit {r.returncode}) {tail[0]}".rstrip())
        return 255 if r.returncode == 255 else 1
    lines, problems = read_hostcheck(r.stdout, only)
    if args.json:
        print(json.dumps({"alias": args.alias, "lines": lines, "problems": problems}))
    else:
        print("\n".join(lines + ["verdict: " + ("problems: " + ", ".join(problems) if problems else "ok")]))
    return 1 if problems else 0


def _mount_spec(args) -> "tuple[dict, dict, dict]":
    """(site, entry, storage spec); SiteError or ValueError when the box has no complete storage answers."""
    site, entry = _entry(args)
    answers = (entry.get("setup") or {}).get("answers") or {}
    mnt = box_storage.spec(answers)
    if not mnt:
        raise ValueError(f"{args.alias} mounts no shared storage (`box.py setup save {args.alias} "
                         "--set storage=<mount point>` to add one)")
    missing = [f"storage.{k}" for k in ("source", "fstype", "method") if not mnt.get(k)]
    if missing:
        raise ValueError(f"ask first ({', '.join(missing)}); `box.py setup questions {args.alias}`")
    return site, entry, mnt


def _ssh_failed(alias: str, r) -> int:
    """Say on stderr why ssh_run gave no answer and return its code. The ledger's daemon reads only a probe's
    stderr: a 255 naming "connection to <alias>" there lets it confirm the box is unreachable (netwait), not
    the probe broken. A timeout stays 124: the box or a mount hangs, which is not ssh failing."""
    tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
    if r.returncode == 255:
        why = f"ssh: connection to {alias} failed (exit 255)"
    elif r.returncode == 124:
        why = f"error: no answer from {alias} in time (exit 124; the box or a mount hangs)"
    else:
        why = f"error: the probe failed on {alias} (exit {r.returncode})"
    print(f"{why} {tail[0]}".rstrip(), file=sys.stderr)
    return r.returncode


def _mount_probe(alias: str, where: str) -> "tuple[Optional[dict], int]":
    r = box.ssh_run(alias, box_storage.probe_script(where), timeout=box_storage.READ_TIMEOUT_S * 2 + 20)
    if r.returncode != 0:
        return None, _ssh_failed(alias, r)
    return box_storage.read_probe(r.stdout), 0


def _record_boot(args, site: dict, entry: dict, where: str, boot: str, **extra) -> None:
    entry.setdefault("setup", {})["storage_verify"] = {
        "where": where, "boot_id": boot,
        "checked": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), **extra}
    save_site(box.site_path(args.site), site)


def _verify_now(args, site: dict, entry: dict, mnt: dict, fresh: bool = False) -> int:
    """Check the mount now; on a pass record the boot id the post-reboot check compares against. A pending
    record is kept (a later check now must not reset it), unless `fresh` (the mount was just (re)made) or
    the last post-reboot check failed: what fixed it is proven only by the next reboot."""
    probe, rc = _mount_probe(args.alias, mnt["where"])
    if probe is None:
        return rc
    lines, problems = box_storage.judge(mnt, probe)
    print("\n".join(lines + ["verdict: " + ("problems: " + ", ".join(problems) if problems else "ok")]))
    if problems:
        return 1
    rec = (entry.get("setup") or {}).get("storage_verify") or {}
    if fresh or rec.get("failed") or rec.get("where") != mnt["where"] or not rec.get("boot_id"):
        _record_boot(args, site, entry, mnt["where"], probe.get("boot_id", ""))
        print(f"recorded this boot; pending: `box.py setup mountcheck {args.alias} --after-reboot` once {args.alias} "
              "has rebooted")
    return 0


def _stable_box_py() -> "tuple[str, bool]":
    """The box.py a long-lived until-probe names, and whether that path may move. The plugin cache this
    runs from is versioned and pruned on upgrades; `ttp setup` keeps a copy at $TTP_HOME/lib/current,
    a link it re-points on every install, so that path (unresolved) survives upgrades."""
    home = os.environ.get("TTP_HOME") or os.path.join(os.path.expanduser("~"), ".tt-project")
    stable = os.path.join(os.path.abspath(home), "lib", "current", "skills", "setup-box", "scripts", "box.py")
    if os.path.isfile(stable):
        return stable, False
    return str(pathlib.Path(__file__).resolve().parent / "box.py"), True


def cmd_mount(args) -> int:
    try:
        site, entry, mnt = _mount_spec(args)
    except (ValueError, box.SiteError) as e:
        print(f"error: {e}")
        return 1
    probe, rc = _mount_probe(args.alias, mnt["where"])
    if probe is None:
        return rc
    mnt, errs = box_storage.resolve(mnt, probe)
    if errs:
        print("\n".join(f"error: {e}" for e in errs))
        return 1
    script = box_storage.apply_script(mnt)
    undo = box_storage.undo_command(args.alias, mnt)
    box_py, moves = _stable_box_py()
    until = shlex.join([box_py, "--site", str(box.site_path(args.site).absolute()),
                        "setup", "mountcheck", args.alias, "--rebooted"])
    ledger = shlex.join(["ttp", "machines", "change", "add", args.alias, box_storage.change_what(args.alias, mnt),
                         "--undo", undo, "--until-probe", until])
    if moves:    # no stable copy (not installed with `ttp setup`): say how to re-point it later
        ledger += (f"\nnote: the probe names {box_py}, a copy a plugin upgrade may remove (the probe then exits "
                   "127): `<plugin-root>/bin/ttp setup` installs the stable copy; then re-point the entry with "
                   "`ttp machines change edit <id> --until-probe ...` and the stable path")
    if not args.run:
        print("dry run; the root script (`--run` runs it with sudo -n):")
        print(script)
        print(f"undo: {undo}")
        print(f"then record it: {ledger}")
        return 0
    r = box.ssh_run(args.alias, "sudo -n bash -c " + shlex.quote(script), timeout=180)
    print((r.stdout + r.stderr).strip())
    if r.returncode == 255:
        return 255
    if r.returncode != 0:
        if "sudo" in r.stderr and ("password" in r.stderr or "not allowed" in r.stderr
                                   or "not in the sudoers" in r.stderr):
            print(f"needs root: sudo -n is refused on {args.alias}. This is a step for the user: run the root script "
                  "from the dry run as root there, then `box.py setup mountcheck` and record the change:\n"
                  f"{ledger}")
        else:
            print(f"error: the root script failed (exit {r.returncode}); nothing else was changed after that point")
        return 1
    rc = _verify_now(args, site, entry, mnt, fresh=True)
    print(f"record the change now: {ledger}")
    return rc


def cmd_mountcheck(args) -> int:
    try:
        site, entry, mnt = _mount_spec(args)
    except (ValueError, box.SiteError) as e:
        print(f"error: {e}", file=sys.stderr if args.rebooted else sys.stdout)
        return 2 if args.after_reboot or args.rebooted else 1
    if not (args.after_reboot or args.rebooted):
        return _verify_now(args, site, entry, mnt)
    rec = (entry.get("setup") or {}).get("storage_verify") or {}
    if rec.get("where") != mnt["where"] or not rec.get("boot_id"):
        print(f"error: no first check recorded for {mnt['where']}: `box.py setup mountcheck {args.alias}` first",
              file=sys.stderr if args.rebooted else sys.stdout)
        return 2
    if args.rebooted:
        # The change entry's until-probe: only the boot id, so it never touches (or hangs on) the mount.
        r = box.ssh_run(args.alias, box_storage.BOOT_ID_SCRIPT, timeout=40)
        if r.returncode == 124:
            # sshd can accept a login and stall mid-boot: not an answer yet, so not over and not broken.
            print(f"pending: no answer from {args.alias} in 40 s (still booting?): not yet")
            return 1
        if r.returncode != 0:
            return _ssh_failed(args.alias, r)
        boot = r.stdout.strip()
        if not boot or boot == rec["boot_id"]:
            print(f"pending: {args.alias} has not rebooted since {rec.get('checked', '?')}")
            return 1
        print(f"{args.alias} rebooted: due, run `box.py setup mountcheck {args.alias} --after-reboot` for the result")
        return 0
    probe, rc = _mount_probe(args.alias, mnt["where"])
    if probe is None:
        return rc
    boot = probe.get("boot_id", "")
    if not boot or boot == rec["boot_id"]:
        print(f"pending: {args.alias} has not rebooted since " + (
            f"the failed post-reboot check ({rec.get('checked', '?')}): fix it, `box.py setup mount {args.alias} "
            f"--run`, then `box.py setup mountcheck {args.alias}`" if rec.get("failed")
            else f"the first check ({rec.get('checked', '?')})"))
        return 1
    lines, problems = box_storage.judge(mnt, probe)
    print("\n".join(lines + ["after reboot: " + ("problems: " + ", ".join(problems) if problems
                                                   else "ok, the mount came back")]))
    if problems:
        # The failed boot is the new baseline: a fix (or a mount by hand) in this boot proves nothing.
        _record_boot(args, site, entry, mnt["where"], boot, failed=",".join(problems))
        return 2
    return 0


def check_setup(alias: str, e: dict) -> List[str]:
    s = e.get("setup")
    if s is None:
        return []
    if not isinstance(s, dict) or not isinstance(s.get("answers", {}), dict) \
            or not isinstance(s.get("asked", {}), dict):
        return [f"{alias}: setup must be an object with answers and asked objects (`box.py setup save` writes it)"]
    return [f"{alias}: setup.answers has unknown question {k!r}" for k in s.get("answers", {}) if k not in BY_ID]


CHECKS = {route: check_setup for route in box.ROUTES}


def register(sub) -> None:
    st = sub.add_parser("setup", help="new-box flow: questions, saved answers, plan, host checks") \
        .add_subparsers(dest="setup_cmd", required=True)
    q = st.add_parser("questions", help="what is still to ask about a box (new, changed or unanswered)")
    q.add_argument("alias")
    q.add_argument("--format", choices=("ask", "numbered"), default="ask",
                   help="ask: batches for a multiple-choice question tool; numbered: a plain prompt")
    q.add_argument("--all", action="store_true", help="ask every question again")
    q.set_defaults(func=cmd_questions)
    s = st.add_parser("save", help="record answers in the box's site entry")
    s.add_argument("alias")
    s.add_argument("--set", action="append", metavar="ID=VALUE", help="one answer (lists comma-separated)")
    s.add_argument("--json", help="answers as one JSON object of question id to value")
    s.set_defaults(func=cmd_save)
    p = st.add_parser("plan", help="each saved answer as an action and its check")
    p.add_argument("alias")
    p.add_argument("--plugin-root", default="<plugin-root>", help="path the printed commands use")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_plan)
    h = st.add_parser("hostcheck", help="read-only: driver, firmware, hugepages, PCIe link")
    h.add_argument("alias")
    h.add_argument("--only", help=f"comma-separated subset of {','.join(HOST_CHECKS)}")
    h.add_argument("--json", action="store_true")
    h.set_defaults(func=cmd_hostcheck)
    j = st.add_parser("journalcheck", help="read-only: whether the login reads the system journal")
    j.add_argument("alias")
    j.add_argument("--save", action="store_true", help="when readable, save journal=readable (skips the question)")
    j.set_defaults(func=cmd_journalcheck)
    m = st.add_parser("mount", help="make the saved shared network mount persistent (dry run unless --run)")
    m.add_argument("alias")
    m.add_argument("--run", action="store_true", help="run the root script with sudo -n")
    m.set_defaults(func=cmd_mount)
    c = st.add_parser("mountcheck", help="verify the shared mount now, or --after-reboot once the box rebooted")
    c.add_argument("alias")
    how = c.add_mutually_exclusive_group()
    how.add_argument("--after-reboot", action="store_true",
                     help="0 came back after a reboot, 1 no reboot yet, 2 failed (that boot becomes the new "
                          "baseline), 124 no answer in time, 255 ssh failed")
    how.add_argument("--rebooted", action="store_true",
                     help="the change entry's until-probe, read-only: 0 rebooted since the baseline (whatever the "
                          "result: run --after-reboot), 1 not yet (also no answer in time, as mid-boot), 255 ssh failed "
                          "(named on stderr)")
    c.set_defaults(func=cmd_mountcheck)
