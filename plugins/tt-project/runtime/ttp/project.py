# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Project layout, configuration and user-level shared state (registry, secrets)."""
from __future__ import annotations

import copy
import json
import os
import re
import socket
import time
from pathlib import Path
from typing import Any

from .db import DB

FOLDER = "tt-project"                     # lives at the root of the user's project, ignored by it
HOME_DIR = Path(os.environ.get("TTP_HOME", Path.home() / ".tt-project"))
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

DEFAULT_CONFIG: dict[str, Any] = {
    "core_provider": "claude",
    "providers": {
        # Tiers name a difficulty class, never a model version: aliases resolve at run time and
        # the project (or the user) pins versions only when it has a reason to.
        "claude": {"tiers": {"light": {"model": "opus", "effort": "low"},
                             "standard": {"model": "opus", "effort": "high"},
                             "deep": {"model": "opus", "effort": "max"}}},
        "codex": {"tiers": {"light": {"model": "", "effort": "low"},
                            "standard": {"model": "", "effort": "high"},
                            "deep": {"model": "", "effort": "xhigh"}}},
        "cursor": {"tiers": {"light": {"model": "auto", "effort": ""},
                             "standard": {"model": "auto", "effort": ""},
                             "deep": {"model": "auto", "effort": ""}}},
    },
    "budget": {
        "reserve_pct": 10,              # plan windows: never take the account past 100 - reserve
        "daily_usd": 100.0,             # applied when the plan reports no window (usage-billed)
        "weekly_usd": 200.0,
        "hourly_alarm_x": 4.0,          # spend rate this many times the 7-day hourly norm = runaway
        "max_parallel_workers": 2,
        "task_default_usd": {"light": 2.0, "standard": 8.0, "deep": 25.0},
        "run_timeout_s": {"light": 1200, "standard": 3600, "deep": 7200},
        "stall_s": {"light": 900, "standard": 1800, "deep": 2700},
    },
    "resources": {},                    # shared-slot limits, e.g. {"device": 1}
    "coordinator": {"tier": "light", "debounce_s": 15, "max_events_per_turn": 40,
                    "max_turns_per_hour": 30, "max_new_tasks_per_day": 40, "idle_wake_s": 3600,
                    "turn_budget_usd": 1.0, "turn_timeout_s": 600},
    "notify": {"slack": False, "slack_min_severity": "high", "chat_min_severity": "normal"},
    "delivery": {"draft_prs": True, "review_before_pr": True, "auto_merge_repos": [],
                 "push_allowed": True},
    "jev": {"enabled": "auto"},
    "web": {"bind": "127.0.0.1", "port": 0},
    "power": {"keep_awake": "on_ac"},
    "screen": {"wake_min_severity": "normal"},
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def hostname() -> str:
    """This machine's short name, restricted to characters that survive markers, ssh and URLs."""
    raw = os.environ.get("TTP_HOST") or socket.gethostname().split(".")[0]
    return re.sub(r"[^A-Za-z0-9._-]", "", raw) or "localhost"


class Project:
    """A project rooted at <root>/tt-project. `harness/` is its own git repo (charter, memory,
    config, prompts, runtime); `state/` holds the database, run directories and logs."""

    def __init__(self, base: str | Path):
        base = Path(base).resolve()
        self.base = base if base.name == FOLDER else base / FOLDER
        self.root = self.base.parent
        self.harness = self.base / "harness"
        self.state = self.base / "state"
        self.runs = self.state / "runs"
        self.logs = self.state / "logs"
        self.worktrees = self.base / "worktrees"
        self.memory_dir = self.harness / "memory"
        self._db: DB | None = None

    # layout -----------------------------------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.harness / "project.json"

    @property
    def charter_path(self) -> Path:
        return self.harness / "CHARTER.md"

    @property
    def memory_index(self) -> Path:
        return self.harness / "MEMORY.md"

    def exists(self) -> bool:
        return self.config_path.is_file()

    @property
    def db(self) -> DB:
        if self._db is None:
            self._db = DB(self.state / "project.db")
        return self._db

    # config -----------------------------------------------------------------------------------
    def raw_config(self) -> dict:
        """The project's own settings. A hand edit that breaks the JSON keeps the last good copy in
        force (and says so in the daemon log) instead of taking the project down."""
        good = self.state / "project.last-good.json"
        try:
            data = json.loads(self.config_path.read_text())
        except FileNotFoundError:
            return {}
        except ValueError:
            try:
                return json.loads(good.read_text())
            except (OSError, ValueError):
                return {}
        try:
            if self.state.is_dir() and (not good.exists() or good.stat().st_mtime < self.config_path.stat().st_mtime):
                write_json(good, data)
        except OSError:
            pass
        return data

    def config(self) -> dict:
        return deep_merge(DEFAULT_CONFIG, self.raw_config())

    def set_config(self, dotted: str, value: Any) -> None:
        raw = self.raw_config()
        node = raw
        parts = dotted.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
        write_json(self.config_path, raw)

    @property
    def name(self) -> str:
        return self.raw_config().get("name", self.root.name)

    # memory -----------------------------------------------------------------------------------
    def add_memory(self, text: str, kind: str = "fact", title: str | None = None) -> Path:
        """One fact per file plus a one-line pointer in MEMORY.md, so the index stays cheap to load."""
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        text = text.strip()
        title = (title or text.splitlines()[0])[:80]
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or "note"
        path = self.memory_dir / f"{kind}-{slug}.md"
        n = 2
        while path.exists():
            path = self.memory_dir / f"{kind}-{slug}-{n}.md"
            n += 1
        path.write_text(f"---\nkind: {kind}\ncreated: {time.strftime('%Y-%m-%d')}\n---\n{text}\n")
        with open(self.memory_index, "a") as f:
            f.write(f"- [{title}](memory/{path.name}) ({kind})\n")
        return path

    def memory_text(self, limit_chars: int = 12000) -> str:
        """Index plus fact bodies, newest last, bounded so it never crowds the coordinator's prompt."""
        if not self.memory_dir.is_dir():
            return ""
        parts = []
        for p in sorted(self.memory_dir.glob("*.md"), key=lambda p: p.stat().st_mtime):
            body = p.read_text().split("---", 2)[-1].strip()
            parts.append(f"[{p.stem}] {body}")
        text = "\n".join(parts)
        return text[-limit_chars:]


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


# user-level registry and secrets ------------------------------------------------------------------
def registry_path() -> Path:
    return HOME_DIR / "registry.json"


def load_registry() -> dict:
    try:
        return json.loads(registry_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"projects": {}}


def register(name: str, entry: dict) -> None:
    reg = load_registry()
    reg.setdefault("projects", {})[name] = {**reg["projects"].get(name, {}), **entry, "updated": time.time()}
    write_json(registry_path(), reg)
    os.chmod(registry_path(), 0o600)


def unregister(name: str) -> None:
    reg = load_registry()
    if reg.get("projects", {}).pop(name, None) is not None:
        write_json(registry_path(), reg)


def secrets_path() -> Path:
    return HOME_DIR / "secrets.json"


def load_secrets() -> dict:
    """Per-user credentials shared by every project of this user on this machine (mode 0600).
    Never copied into a project folder, never printed."""
    try:
        return json.loads(secrets_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_secret(key: str, value: Any) -> None:
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(HOME_DIR, 0o700)
    data = load_secrets()
    data[key] = value
    tmp = secrets_path().with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, secrets_path())
