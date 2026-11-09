# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A virtualenv of its own for each bench cell (`ttp bench run`), so nothing a cell runs installs into
or imports from the user site (~/.local) that every project on the host shares.

make() builds a fresh venv in the cell's own folder (`python -m venv --without-pip`, its pyvenv.cfg
saying include-system-site-packages = false, so every pip version refuses `--user` there; a .pth
file adds the OS's site directories below its own, so the OS's packages, pip among them, stay
visible; ensurepip adds pip where the OS has none) and copies into it the distributions it names that this interpreter has
installed, with what they require (pytest by default): the cell gets today's copy, never a live
path into the user site. env() gives the environment every process of the cell runs with: the
venv's bin first on PATH, VIRTUAL_ENV set, PYTHONNOUSERSITE=1 (the user site never loads),
PIP_REQUIRE_VIRTUALENV=1 (pip refuses to run outside a venv), PYTHONUSERBASE in the cell's folder
(a `--user` install that gets past both lands there) and PIP_USER, PYTHONHOME, PYTHONPATH,
PYTEST_ADDOPTS and PYTEST_PLUGINS unset."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

SEED = ("pytest",)
# PYTEST_ADDOPTS and PYTEST_PLUGINS: a cell's pytest runs as on its own, never with the options or
# plugins (and their import paths) of a pytest that started the run.
DROP = ("PIP_USER", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "PYTEST_ADDOPTS", "PYTEST_PLUGINS")


class Unavailable(RuntimeError):
    """The cell's venv could not be built; the cell must not run without it."""


def make(path: Path, seed=SEED, timeout_s: float = 300) -> Path:
    """Build a fresh venv at `path` (refused when something is there) holding `seed`; returns `path`."""
    path = Path(path)
    if path.exists():
        raise Unavailable(f"{path} already exists: a cell's venv is never reused")
    base = {k: v for k, v in os.environ.items() if k not in DROP}
    try:
        # Not --system-site-packages: pip refuses an explicit --user only in a venv that hides the
        # global site (pip 22 lets it through in one that shows it, #18 on blx06).
        r = subprocess.run([_base_python(), "-m", "venv", "--without-pip", str(path)],
                           capture_output=True, text=True, timeout=timeout_s, stdin=subprocess.DEVNULL, env=base)
        if r.returncode:
            raise Unavailable(f"python -m venv {path} failed: {(r.stderr or r.stdout).strip()[-500:]}")
        r = subprocess.run([_base_python(), "-s", "-c", "import site; print(repr(site.getsitepackages()))"],
                           capture_output=True, text=True, timeout=timeout_s, stdin=subprocess.DEVNULL, env=base)
        if r.returncode:
            raise Unavailable(f"the OS's site directories could not be read: {(r.stderr or r.stdout).strip()[-500:]}")
        for site in path.glob("lib/python*/site-packages"):
            # addsitedir, not bare paths: the OS's own .pth files (namespace packages) still apply. map,
            # not a comprehension: before 3.12 one cannot see `site` in the exec'd line's scope.
            (site / "ttp-system-site.pth").write_text(
                f"import site; list(map(site.addsitedir, {r.stdout.strip()}))\n")
        cell = env(path, base)
        py = str(path / "bin" / "python")
        if subprocess.run([py, "-c", "import pip"], capture_output=True, timeout=timeout_s, env=cell).returncode:
            r = subprocess.run([py, "-m", "ensurepip", "--default-pip"], capture_output=True, text=True,
                               timeout=timeout_s, stdin=subprocess.DEVNULL, env=cell)
            if r.returncode:
                raise Unavailable(f"no pip in {path}: {(r.stderr or r.stdout).strip()[-500:]}")
    except (OSError, subprocess.SubprocessError) as e:
        raise Unavailable(f"the venv {path} could not be built: {e}") from e
    sites = sorted(path.glob("lib/python*/site-packages"))
    if not sites:
        raise Unavailable(f"the venv {path} has no site-packages")
    _copy(seed, sites[0])
    return path


def _base_python() -> str:
    # The interpreter outside any venv this one runs in: a venv made from a venv's python is not whole.
    v = sys.version_info
    for c in ((Path(sys.base_prefix) / "bin" / f"python{v.major}.{v.minor}", Path(sys.base_prefix) / "bin" / "python3")
              if sys.prefix != sys.base_prefix else ()):
        if c.exists():
            return str(c)
    return sys.executable


def _copy(names, site: Path) -> None:
    """Copy each installed distribution of `names`, and those it requires, into `site`."""
    todo, done = list(names), set()
    while todo:
        name = re.sub(r"[-_.]+", "-", todo.pop()).lower()
        if name in done:
            continue
        done.add(name)
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        for f in dist.files or []:
            if ".." in f.parts or f.suffix == ".pyc":   # scripts in another bin; caches rebuild
                continue
            src = Path(dist.locate_file(f))
            if src.is_file():
                (site / f).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, site / f)
        todo += [m.group(0) for r in dist.requires or [] if not re.search(r";.*\bextra\s*==", r)
                 if (m := re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", r))]


def env(path: Path, base: dict, userbase: Path | None = None) -> dict:
    """`base` as a process of the cell whose venv is `path` runs with."""
    path = Path(path)
    out = {k: v for k, v in base.items() if k not in DROP}
    return {**out, "PATH": os.pathsep.join(p for p in (str(path / "bin"), base.get("PATH", "")) if p),
            "VIRTUAL_ENV": str(path), "PYTHONNOUSERSITE": "1", "PIP_REQUIRE_VIRTUALENV": "1",
            "PYTHONUSERBASE": str(userbase or path.parent / f"{path.name}-userbase")}
