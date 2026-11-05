# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Keeps Claude Code's tt-project marketplace on the release `ttp setup` installs, so interactive
sessions load the same skills as the daemons and `ttp`.

Each installed release (~/.tt-project/lib/<version>) is also a one-plugin directory marketplace
(write()). A Claude Code registration of the tenstorrent-skills marketplace that is a directory
holding an older tt-project (an installer clone that daemon upgrades never update) is repointed at
~/.tt-project/lib/current, which every later setup moves on. The settings files are backed up
before an edit, and then Claude Code's plugin cache is refreshed. A marketplace that is not
tt-project's, or one that also serves another installed or enabled plugin, is left as it is."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

MARKETPLACE = "tenstorrent-skills"
PLUGIN = "tt-project"
ID = f"{PLUGIN}@{MARKETPLACE}"
REFRESH_TIMEOUT_S = 180


def write(lib: Path) -> None:
    """Make the release folder `lib` a directory marketplace holding just this plugin."""
    from .project import durable_write
    text = json.dumps({"name": MARKETPLACE, "owner": {"name": "tenstorrent"},
                       "description": "tt-project as installed by `ttp setup`",
                       "plugins": [{"name": PLUGIN, "source": "./"}]}, indent=2) + "\n"
    durable_write(lib / ".claude-plugin" / "marketplace.json", text, sync=False)


def _load(f: Path) -> dict | None:
    try:
        d = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def plugin_version(path: str) -> str:
    """The tt-project version the directory marketplace at `path` serves; "" when it serves none."""
    root = Path(path).expanduser()
    m = _load(root / ".claude-plugin" / "marketplace.json") or {}
    for p in m.get("plugins") if isinstance(m.get("plugins"), list) else []:
        if isinstance(p, dict) and p.get("name") == PLUGIN and isinstance(p.get("source"), str):
            v = (_load(root / p["source"] / ".claude-plugin" / "plugin.json") or {}).get("version")
            return v if isinstance(v, str) else ""
    return ""


def _dir_path(entry) -> str:
    src = entry.get("source") if isinstance(entry, dict) else None
    if isinstance(src, dict) and src.get("source") == "directory" and isinstance(src.get("path"), str):
        return src["path"]
    return ""


def _regs(key: str, d: dict | None) -> dict:
    """The marketplace registrations, by name, of settings.json or known_marketplaces.json."""
    return _dict((d or {}).get("extraKnownMarketplaces") if key == "settings" else d)


def _dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _others(settings: dict, installed: dict) -> list[str]:
    """Other plugins of the marketplace that are enabled or installed: repointing would drop them."""
    ids = [k for k, on in _dict(settings.get("enabledPlugins")).items() if on]
    ids += list(_dict(installed.get("plugins")).keys())
    return sorted({i for i in ids if isinstance(i, str) and i.endswith("@" + MARKETPLACE) and i != ID})


def _backup(f: Path) -> Path:
    b = f.with_name(f"{f.name}.ttp-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(f, b)
    return b


def _installed_version(installed: dict) -> str | None:
    """The version of tt-project in Claude Code's plugin cache; None when it is not installed."""
    rows = _dict(installed.get("plugins")).get(ID)
    vs = [r.get("version") for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict) and isinstance(r.get("version"), str)]
    return max(vs, key=lambda v: tuple(int(x) for x in v.split(".") if x.isdigit())) if vs else None


def reconcile(current: Path, version: str, claude_dir: Path | None = None) -> list[str]:
    """Repoint a stale tt-project marketplace at `current` (the installed release, `version`) and
    refresh the plugin cache. Lines for setup to print; none when nothing changed."""
    from .project import durable_write
    from .release import is_newer
    if claude_dir is None:
        from .providers.claude import claude_config_dir
        claude_dir = claude_config_dir()
    if not claude_dir.is_dir():
        return []
    files = {"settings": claude_dir / "settings.json",
             "known": claude_dir / "plugins" / "known_marketplaces.json"}
    data = {k: _load(f) for k, f in files.items()}
    installed = _load(claude_dir / "plugins" / "installed_plugins.json") or {}
    out: list[str] = []
    stale = {}      # file key -> the registered path to replace
    for k, d in data.items():
        path = _dir_path(_regs(k, d).get(MARKETPLACE))
        if path and path != str(current) and (v := plugin_version(path)) and is_newer(version, v):
            stale[k] = (path, v)
    if stale:
        others = _others(data["settings"] or {}, installed)
        old = ", ".join(sorted({f"{p} ({v})" for p, v in stale.values()}))
        if others:
            return [f"Claude Code: the {MARKETPLACE} marketplace at {old} is older than tt-project {version}, "
                    f"but it also serves {', '.join(others)}, so it was left as is; rerun the tt-project "
                    f"installer to update it"]
        for k in stale:
            f, d = files[k], data[k]
            entry = _regs(k, d)[MARKETPLACE]
            entry["source"]["path"] = str(current)
            if k == "known" and "installLocation" in entry:
                entry["installLocation"] = str(current)
            try:
                b = _backup(f)
                durable_write(f, json.dumps(d, indent=2) + "\n")
            except OSError as e:
                return out + [f"Claude Code: could not repoint the {MARKETPLACE} marketplace in {f} ({e}); "
                              f"rerun `ttp setup` where that file is writable"]
            out.append(f"Claude Code: {f.name} pointed the {MARKETPLACE} marketplace at {stale[k][0]} ({stale[k][1]}); "
                       f"now at {current} (backup: {b.name})")
    have = _installed_version(installed)
    # Only a cache behind a registration that serves this release: never a pull from another source each setup.
    ours = any((p := _dir_path(_regs(k, d).get(MARKETPLACE))) and plugin_version(p) == version
               for k, d in data.items())
    if stale or (ours and have is not None and is_newer(version, have)):
        out += refresh(have, version)
    return out


def refresh(have: str | None, version: str) -> list[str]:
    """Refresh Claude Code's marketplace and its cached copy of the plugin with the `claude` CLI."""
    claude = shutil.which("claude")
    # Never installs it: a user who has not installed the plugin gets only the marketplace refreshed.
    cmds = [["plugin", "marketplace", "update", MARKETPLACE]] + ([["plugin", "update", ID]] if have else [])
    if not claude:
        return [f"Claude Code: run `{' && '.join('claude ' + ' '.join(c) for c in cmds)}` "
                f"to load tt-project {version} in new sessions (no `claude` on PATH here)"]
    for c in cmds:
        try:
            r = subprocess.run([claude, *c], capture_output=True, text=True, timeout=REFRESH_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            return [f"Claude Code: `claude {' '.join(c)}` failed ({e}); the next `ttp setup` retries it"]
        if r.returncode:
            tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
            return [f"Claude Code: `claude {' '.join(c)}` exited {r.returncode} ({tail[0][:200]}); "
                    f"the next `ttp setup` retries it"]
    if not have:
        return [f"Claude Code: the {MARKETPLACE} marketplace refreshed to tt-project {version}"]
    return [f"Claude Code: plugin cache refreshed to tt-project {version} (was {have}); new sessions load it"]
