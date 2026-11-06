# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Keeps Codex's installed copy of tt-project on the release `ttp setup` installs, so Codex workers
and coordinators load the skills of the version the runtime runs (claudemarket.py does this for
Claude Code).

Codex loads a plugin only from its install cache, `$CODEX_HOME/plugins/cache/<marketplace>/
tt-project/<version>`, which only `codex plugin add` fills, from the source config.toml registers
for the marketplace. Each installed release (~/.tt-project/lib/<version>) is also a one-plugin Codex
marketplace (write()). When the cache holds another version than the release, reconcile() runs
`codex plugin add` once. When the registered folder itself serves another version (an installer
clone nothing updates), it first moves the registration to the release, unless that marketplace also
serves another installed or enabled plugin or a newer tt-project (a developer's own checkout). Without `codex` on PATH or a registered tt-project
marketplace it does nothing; a failed or hung call is a line to print, never fatal."""
from __future__ import annotations

import fcntl
import importlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .claudemarket import MARKETPLACE, PLUGIN

ID = f"{PLUGIN}@{MARKETPLACE}"
CALL_TIMEOUT_S = 180
MANIFEST = Path(".agents") / "plugins" / "marketplace.json"


def write(lib: Path) -> None:
    """Make the release folder `lib` a Codex marketplace holding just this plugin."""
    from .project import durable_write
    text = json.dumps({"name": MARKETPLACE, "interface": {"displayName": "tt-project as installed by `ttp setup`"},
                       "plugins": [{"name": PLUGIN, "source": {"source": "local", "path": "./"},
                                    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}}]},
                      indent=2) + "\n"
    durable_write(lib / MANIFEST, text, sync=False)


def _load(f: Path):
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


def plugin_version(path: str) -> str:
    """The tt-project version the local Codex marketplace at `path` serves; "" when it serves none."""
    root = Path(path).expanduser()
    m = _load(root / MANIFEST)
    for p in m.get("plugins") if isinstance(m, dict) and isinstance(m.get("plugins"), list) else []:
        if not (isinstance(p, dict) and p.get("name") == PLUGIN):
            continue
        src = p.get("source")
        rel = src.get("path") if isinstance(src, dict) else src
        if isinstance(rel, str):
            v = _load(root / rel / ".codex-plugin" / "plugin.json")
            return v["version"] if isinstance(v, dict) and isinstance(v.get("version"), str) else ""
    return ""


def _keys(path: str) -> list[str]:
    """A TOML dotted key's parts: bare, "basic" (escapes undone) or 'literal'."""
    return [_basic(m.group(1)) if m.lastindex == 1 else m.group(m.lastindex)
            for m in re.finditer(r'"((?:[^"\\]|\\.)*)"|\'([^\']*)\'|([A-Za-z0-9_-]+)', path)]


def _basic(body: str) -> str:
    try:
        return json.loads(f'"{body}"')
    except ValueError:   # a TOML-only escape (\e, \U...): kept as written
        return body


def _sub(t: dict, k: str) -> dict:
    if not isinstance(t.get(k), dict):
        t[k] = {}
    return t[k]


def config(home: Path) -> dict:
    """Codex's config.toml as a dict: by tomllib or tomli, else the plain tables and string or boolean
    keys `codex plugin` writes; {} when it is missing or does not parse."""
    try:
        text = (home / "config.toml").read_text(errors="replace")
    except OSError:
        return {}
    for name in ("tomllib", "tomli"):
        try:
            return importlib.import_module(name).loads(text)
        except ImportError:
            continue
        except Exception:   # TOMLDecodeError: nothing in it counts
            return {}
    out: dict = {}
    table = out
    for line in text.splitlines():
        if h := re.fullmatch(r"\s*\[\s*([^\[\]]+?)\s*\]\s*(?:#.*)?", line):
            table = out
            for k in _keys(h.group(1)):
                table = _sub(table, k)
        elif line.lstrip().startswith("["):
            table = {}   # an array of tables, or a header this reader does not take: its keys count nowhere
        elif kv := re.fullmatch(r"\s*([^=#\s][^=]*?)\s*=\s*(\"(?:[^\"\\]|\\.)*\"|'[^']*'|true|false)\s*(?:#.*)?", line):
            *parents, last = _keys(kv.group(1))
            t = table
            for k in parents:
                t = _sub(t, k)
            v = kv.group(2)
            t[last] = v == "true" if v in ("true", "false") else _basic(v[1:-1]) if v[0] == '"' else v[1:-1]
    return out


def _dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def cached(home: Path, market: str = MARKETPLACE) -> list[str]:
    """The tt-project versions in Codex's install cache for `market`, newest first."""
    try:
        vs = [d.name for d in (home / "plugins" / "cache" / market / PLUGIN).iterdir() if d.is_dir()]
    except OSError:
        return []
    return sorted(vs, key=lambda v: tuple(int(x) for x in re.findall(r"\d+", v)), reverse=True)


def _others(home: Path, cfg: dict) -> list[str]:
    """Other plugins of the marketplace that are enabled or installed: moving it would drop them."""
    ids = [k for k, e in _dict(cfg.get("plugins")).items() if not (isinstance(e, dict) and e.get("enabled") is False)]
    try:
        ids += [f"{d.name}@{MARKETPLACE}" for d in (home / "plugins" / "cache" / MARKETPLACE).iterdir() if d.is_dir()]
    except OSError:
        pass
    return sorted({i for i in ids if i.endswith("@" + MARKETPLACE) and i != ID})


def _call(codex: str, *args: str) -> str:
    """Run `codex <args>`; "" when it exited 0, else why not."""
    try:
        r = subprocess.run([codex, *args], capture_output=True, text=True, timeout=CALL_TIMEOUT_S,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"`codex {' '.join(args)}` failed ({e})"
    if r.returncode:
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
        return f"`codex {' '.join(args)}` exited {r.returncode} ({tail[0][:200]})"
    return ""


def reconcile(current: Path, version: str, home: Path | None = None) -> list[str]:
    """Bring Codex's cached tt-project to `version`, the release installed at `current`. Lines to
    print; none when the cache already holds it, or Codex or its tt-project marketplace is missing."""
    from .project import HOME_DIR
    from .providers.codex import codex_home
    from .release import is_newer
    home = home or codex_home()
    cfg = config(home)
    reg = _dict(cfg.get("marketplaces")).get(MARKETPLACE)
    codex = shutil.which("codex")
    have = (cached(home) or [None])[0]
    entry = _dict(cfg.get("plugins")).get(ID)
    installed = have is not None or (isinstance(entry, dict) and entry.get("enabled") is not False)
    if not isinstance(reg, dict) or not codex or not installed or have == version:
        return []
    why = f"Codex: its tt-project cache holds {have or 'nothing'}, the release is {version}"
    try:
        HOME_DIR.mkdir(parents=True, exist_ok=True)
        lock = os.open(HOME_DIR / ".codexmarket.lock", os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        return [f"{why}; not refreshed ({e})"]
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return [f"{why}; another `ttp setup` or `ttp doctor` is refreshing it"]
        out: list[str] = []
        src = reg.get("source")
        if reg.get("source_type", "local") != "local" or not isinstance(src, str):
            return [f"{why}; its {MARKETPLACE} marketplace is not a local folder, so it was left as is: run "
                    f"`codex plugin marketplace upgrade {MARKETPLACE} && codex plugin add {ID}`"]
        served = plugin_version(src)
        if served and is_newer(served, version):   # a newer checkout of the user's own: theirs to keep
            return [f"{why}; its {MARKETPLACE} marketplace at {src} serves the newer {served}, so it was left as is"]
        if served != version:
            if plugin_version(str(current)) != version:
                return [f"{why}; {current} is no Codex marketplace of it: rerun `ttp setup` from the plugin"]
            if others := _others(home, cfg):
                return [f"{why}; its {MARKETPLACE} marketplace at {src} ({served or 'no tt-project'}) also serves "
                        f"{', '.join(others)}, so it was left as is: rerun the tt-project installer to update it"]
            if err := _call(codex, "plugin", "marketplace", "remove", MARKETPLACE):
                return [f"{why}; {err}; the next `ttp setup` or `ttp doctor` retries it"]
            if err := _call(codex, "plugin", "marketplace", "add", str(current)):
                back = _call(codex, "plugin", "marketplace", "add", src)
                return [f"{why}; {err}; " + (f"{src} is registered again" if not back else
                        f"and putting {src} back: {back}; run `codex plugin marketplace add {src}`")]
            out.append(f"Codex: the {MARKETPLACE} marketplace served tt-project {served or 'nothing'} from {src}; "
                       f"now from {current}")
        if err := _call(codex, "plugin", "add", ID):
            return out + [f"{why}; {err}; the next `ttp setup` or `ttp doctor` retries it"]
        return out + [f"Codex: plugin cache refreshed to tt-project {version} (was {have or 'none'}); "
                      f"new sessions load it"]
    finally:
        os.close(lock)


def doctor_lines() -> list[str]:
    """`ttp doctor`'s Codex plugin lines: reconcile() on the installed release (so doctor fixes a lag
    too), else whether the cache matches it; none without codex or its tt-project marketplace."""
    from .providers.codex import codex_home
    from .release import installed, runtime_version
    current = installed()
    version = runtime_version(current / "runtime")
    home = codex_home()
    if version == "unknown" or not shutil.which("codex") or not isinstance(
            _dict(config(home).get("marketplaces")).get(MARKETPLACE), dict):
        return []
    lines = reconcile(current, version, home)
    have = (cached(home) or ["nothing"])[0]
    return lines or [f"codex plugin cache: tt-project {have}" + (", as the installed release" if have == version
                     else f" (not installed into Codex; `codex plugin add {ID}` adds it)")]
