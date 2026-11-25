# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
# The static proof of inline Python (pyproof.py) and the hook's use of it (hook._proven), of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
from __future__ import annotations

_SSH = "." + "ssh"

_PROVEN = [
    "import json,sys; d=json.load(sys.stdin); print(len(d))",
    "import json; print(json.load(open('state/x.json'))['a'])",
    "from pathlib import Path\nfor l in Path('a.txt').read_text().splitlines(): print(l)",
    "import re,collections\nc=collections.Counter()\nprint(c.most_common(3))",
    f"s='~/{_SSH}/id_rsa'\nprint(s.replace('a','b'))",          # a credential path as a string only
    "open('out/x.txt','w').write('hi')",                           # a write inside the worktree
    "from os.path import join\nprint(join('a','b'))",
    "import os\nprint(os.listdir('.'))",
    "print('{} {}'.format(1,2))",
    "import glob\nprint(glob.glob('src/*.py'))", "from pathlib import Path\nprint(sorted(Path('.').glob('*.md')))",
    "for _ in range(3):\n    print(dict(a=1).get('a'))",
]

_UNPROVEN = [
    "import os; os.system('id')", "import subprocess", "__import__('os')", "eval('1')", "exec('x')",
    "import ast; ast.sys.modules", "import shlex\nprint(shlex.os)",              # a module reached through another
    "open('/etc/passwd','w')", f"open('/x/{_SSH}/id_rsa').read()", "open('/proc/self/environ').read()",
    f"import os\nopen(os.path.expanduser('~/{_SSH}/id_rsa'))", f"open('~/{_SSH}/id_rsa').read()",
    f"from pathlib import Path\np=Path.home()/'{_SSH}'/'id_rsa'\nprint(p.read_text())",
    "p='x'+'y'\nopen(p)", "import sys\nopen(sys.argv[1])",                     # computed paths
    "from pathlib import Path\nimport sys\nPath(sys.argv[1]).read_text()",
    "import json; getattr(json,'loads')", "x=().__class__", "f=open; f('a','w')",  # getattr and alias tricks
    "import os as o; o=1", "from os import system", "import operator; operator.attrgetter('a')",
    "'{0.__globals__}'.format(print)", "fmt='{}'\nfmt.format(1)",
    "import functools\ndef g(x: 'evil') -> None: pass",
    "from pathlib import Path\nPath('x').write_text('y')", "from pathlib import Path\nPath('a').replace('b')",
    "import time; time.sleep(100)", "import socket", "import ctypes", "import importlib", "import os; os.environ['A']",
    "not python (",
    # modules under other names, frame objects, rebound literal names, keyword modes, listings
    "from shlex import os\nos.system('id')", "import os\nos.path.os.system('id')", "import base64; base64.main()",
    "def g(): yield\nprint(g().gi_frame.f_builtins)", "p='a.txt'\nf=lambda p: open(p,'w')\nf('/etc/x')",
    "p='a.txt'\ndef w(p): open(p,'w')\nw('/etc/x')", "p='a.txt'\nmatch '/etc/x':\n    case p: open(p,'w')",
    "p='a.txt'\ntry: pass\nexcept Exception as p: pass\nopen(p,'w')",
    "from pathlib import Path\nPath('/etc/x').open(mode='a')", "from pathlib import Path\nPath('a').replace(target='/etc/x')",
    "import shlex\nshlex.shlex('').sourcehook('/etc/shadow')", f"import os\nos.listdir('/x/{_SSH}')",
    f"import glob\nglob.glob('/x/{_SSH}/*')", "import os\nos.listdir(input())", "x=print\nx.__self__",
    "import json\njson.JSONDecoder.decode.__globals__", "def f(): eval('1')\neval = print",
    "class A: pass\nA.x = 1",
]


def test_pyproof_proves_plain_reads_and_worktree_writes_and_nothing_else(tmp_path, monkeypatch):
    from ttp import pyproof
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for code in _PROVEN:
        assert pyproof.harmless(code, str(tmp_path)), code
    for code in _UNPROVEN:
        assert not pyproof.harmless(code, str(tmp_path)), code


def test_pyproof_rewrites_only_the_proven_snippet_and_keeps_the_rest_of_the_line(tmp_path, monkeypatch):
    from ttp import pyproof
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cwd = str(tmp_path)
    proven = "import json,sys; print(json.load(sys.stdin))"
    assert pyproof.rewrite(f"cat a.json | python3 -c '{proven}' && rm -rf x", cwd) == \
        "cat a.json | python3 -c 'pass' && rm -rf x"
    assert pyproof.rewrite(f"python3 - <<'EOF'\n{proven}\nEOF\necho done", cwd) == "python3 - <<'EOF'\npass\nEOF\necho done"
    assert pyproof.rewrite(f"python3 - <<EOF\n{proven}\nEOF", cwd) == "python3 - <<EOF\npass\nEOF"
    assert pyproof.rewrite(f"python3 - <<-'EOF'\n\t{proven}\n\tEOF", cwd) == "python3 - <<-'EOF'\npass\n\tEOF"
    for cmd in (f"python3 - <<EOF\nprint('$(cat x)')\nEOF",                    # the shell expands an unquoted heredoc
                'python3 -c "print(\'$HOME\')"',                                 # and a double-quoted -c
                f"python3 -c 'import os; os.system(\"id\")'",
                f"cat <<'EOF'\n{proven}\nEOF",                                   # not fed to python
                f"python3 x.py -c '{proven}'",                                   # the script's -c, not python's
                f"python3 - <<'EOF'\n{proven}\n",                                # never ended
                "cd /etc && python3 -c 'open(\"hosts\",\"w\").write(\"x\")'",          # a relative write after a cd
                "echo hi"):
        assert pyproof.rewrite(cmd, cwd) is None, cmd


def test_the_hook_passes_a_proven_snippet_and_still_refuses_the_rest_of_its_call(env, tmp_path, monkeypatch):
    home = _cred_home(tmp_path, monkeypatch)
    key = home / _SSH / "id_ed25519"
    named = f"python3 - <<'EOF'\nprint('{key}'.split('/')[-1])\nEOF"
    assert _bash_why(tmp_path, named) == ""                                     # names a store, reads nothing
    assert "credential" in _bash_why(tmp_path, f"python3 - <<'EOF'\nprint(open('{key}').read())\nEOF")
    assert _bash_why(tmp_path, f"python3 -c \"print(1)\"; cat {key}")           # the rest of the line is judged
    for cmd in (f"python3 -c 'print(\"cat {key}\")' | sh", f"K=$(python3 -c 'print(\"{key}\")'); cat \"$K\"",
                f"python3 -c 'print(\"{key}\")' | xargs cat", f"python3 -c 'open(\"s.sh\",\"w\").write(\"cat {key}\")' && sh s.sh",
                f"python3 - <<'EOF' | bash\nprint('cat {key}')\nEOF"):
        assert _bash_why(tmp_path, cmd), cmd                                      # its output may feed the name to the shell


def _store_paths(home):
    """The web token's and the secrets store's paths under a fake HOME, spelled so this file never names them."""
    return [f"{home}/.tt-project/state/" + "web." + "token", f"{home}/.tt-project/" + "secrets" + ".json"]


def test_the_hook_refuses_a_snippet_that_opens_the_web_token_or_the_secrets_store(env, tmp_path, monkeypatch):
    import os
    home = _cred_home(tmp_path, monkeypatch)
    for path in _store_paths(home):
        for cmd in (f"python3 -c 'print(open(\"{path}\").read())'",
                    f"python3 -c 'from pathlib import Path\nprint(Path(\"{path}\").read_text())'",
                    f"python3 - <<'EOF'\nprint(open('{path}').read())\nEOF",
                    f"python3 - <<'EOF'\nfrom pathlib import Path\nprint(Path('{path}').open().read())\nEOF"):
            assert _bash_why(tmp_path, cmd), cmd
        from ttp import pyproof   # a relative name after a cd: the snippet is not freed, the guards read it as written
        assert pyproof.rewrite(f"cd {os.path.dirname(path)} && python3 -c 'print(open(\"{os.path.basename(path)}\").read())'",
                               str(tmp_path)) is None
    assert _bash_why(tmp_path, f"python3 -c 'print(open(\"{home}/notes.txt\").read())'") == ""   # other files stay free


def test_the_hook_refuses_a_store_naming_snippet_that_writes_a_file_for_a_later_call(env, tmp_path, monkeypatch):
    home = _cred_home(tmp_path, monkeypatch)
    key = home / _SSH / "id_ed25519"
    for mode in ("w", "a", "x", "w+", "r+"):
        for cmd in (f"python3 -c 'open(\"s.sh\",\"{mode}\").write(\"cat {key}\")'",
                    f"python3 -c 'open(\"s.sh\",mode=\"{mode}\").write(\"cat {key}\")'",
                    f"python3 - <<'EOF'\nopen('s.sh','{mode}').write('cat {key}')\nEOF",
                    f"python3 - <<-'EOF'\n\topen('s.sh','{mode}').write('cat {key}')\n\tEOF",
                    f"python3 - <<'EOF'\nfrom pathlib import Path\nPath('s.sh').open('{mode}').write('cat {key}')\nEOF"):
            assert _bash_why(tmp_path, cmd), cmd
    assert _bash_why(tmp_path, "python3 -c 'open(\"s.sh\",\"w\").write(\"echo hi\")'") == ""   # no store named
    for path in _store_paths(home):   # the web token and the secrets store, named only: the same three routes
        for cmd in (f"python3 -c 'print(\"cat {path}\")' | sh", f"python3 -c 'open(\"s.sh\",\"w\").write(\"cat {path}\")'",
                    f"K=$(python3 -c 'print(\"{path}\")'); cat \"$K\""):
            assert _bash_why(tmp_path, cmd), cmd


def test_pyproof_reads_heredocs_as_the_hook_does(tmp_path, monkeypatch):
    from ttp import pyproof
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cwd, code = str(tmp_path), "print(1)"
    assert pyproof.rewrite(f"python3 - <<'A' && python3 - <<'B'\n{code}\nA\n{code}\nB", cwd) == \
        "python3 - <<'A' && python3 - <<'B'\npass\nA\npass\nB"                    # two on one line, each its own body
    assert pyproof.rewrite(f"python3 - <<-'EOF'\n\t{code}\n\t\tEOF\necho x", cwd) == \
        "python3 - <<-'EOF'\npass\n\t\tEOF\necho x"                               # the end line stays as written
    assert pyproof.rewrite(f"python3 - <<'EOF'\n{code}\nEOF\npython3 - <<'EOF'\n{code}\n", cwd) is None   # one unended
    assert pyproof.rewrite(f"python3 - <<'EOF'\n{code}\n EOF\n", cwd) is None   # an indented end line does not end it


def test_cwds_reads_a_ten_thousand_line_heredoc_quickly(env, tmp_path):
    import time
    from ttp import credguard, hook
    body = "\n".join(f"print('{i}', '/tmp/x{i}', '-o{i}', 'a/b/c{i}')" for i in range(10000))
    cmd = f"cd {tmp_path} && python3 - <<'EOF'\n{body}\nEOF"
    segments = hook._segments(cmd, strip=False)
    t = time.perf_counter()
    cwds = credguard._cwds(segments, str(tmp_path), str(tmp_path / "home"))
    assert time.perf_counter() - t < 0.2
    assert str(tmp_path) in cwds
    from ttp import pyproof
    hops = "; ".join(f"cd a{i % 3}" for i in range(80))                         # each relative cd doubles the folders
    t = time.perf_counter()
    cwds = credguard._cwds(hook._segments(f"{hops}; cat x", strip=False), str(tmp_path), str(tmp_path / "home"))
    assert time.perf_counter() - t < 0.2 and len(cwds) <= credguard.CWDS_CAP + 1 and str(tmp_path / "home") in cwds[1:]  # past the cap, read from HOME too
    assert pyproof.rewrite(f"{hops}; python3 -c 'print(1)'", str(tmp_path)) is None


def test_pyproof_proves_nothing_when_python_may_import_a_module_from_its_folder_or_pythonpath(tmp_path, monkeypatch):
    import importlib.machinery
    from ttp import pyproof
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    code = "import json; print(json.dumps(1))"
    cmds = {f"python3 -c '{code}'": "python3 -c 'pass'", f"python3 - <<'EOF'\n{code}\nEOF": "python3 - <<'EOF'\npass\nEOF"}

    def proven(where):
        return {cmd: pyproof.rewrite(cmd, str(where)) for cmd in cmds} == cmds
    for n, planted in enumerate(("json.py", "re.py", "json.pyc", "json" + importlib.machinery.EXTENSION_SUFFIXES[0],
                                 "json/__init__.py")):   # the module, one it imports, each form python loads
        work, lib = tmp_path / f"w{n}", tmp_path / f"lib{n}"
        (work / "json").mkdir(parents=True)              # a folder alone is a namespace part: the standard json wins
        (work / "notes.txt").write_text("x")
        assert proven(work), planted                     # a clean folder
        (lib / planted).parent.mkdir(parents=True, exist_ok=True)
        (lib / planted).write_text("x")
        assert not proven(lib), planted                  # planted in the working folder
        monkeypatch.setenv("PYTHONPATH", str(lib))
        assert not proven(work), planted                 # or in a PYTHONPATH entry
        monkeypatch.setenv("PYTHONPATH", f"{tmp_path / 'none'}:{lib.name}")
        assert not proven(tmp_path), planted             # a relative one too
        monkeypatch.delenv("PYTHONPATH")
        assert pyproof.rewrite(f"cd {lib} && python3 -c '{code}'", str(work)) is None, planted   # or where a cd goes
    work = tmp_path / "w0"
    for cmd in (f"PYTHONPATH=x python3 -c '{code}'", f"export PYTH\"\"ONPATH=x; python3 -c '{code}'",
                f". ./env.sh && python3 -c '{code}'", f"export \"$V\"; python3 -c '{code}'",
                f"cd \"$D\" && python3 -c '{code}'", f"cd - && python3 -c '{code}'"):
        assert pyproof.rewrite(cmd, str(work)) is None, cmd   # it may set Python's environment or folder


def test_the_hook_refuses_a_store_naming_snippet_beside_a_module_named_like_one_it_imports(env, tmp_path, monkeypatch):
    home = _cred_home(tmp_path, monkeypatch)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    key = home / _SSH / "id_ed25519"
    cmd = f"python3 - <<'EOF'\nimport difflib\nprint(difflib.get_close_matches('{key}', []))\nEOF"
    assert _bash_why(tmp_path, cmd) == ""                                       # the standard difflib: it names only
    for planted in ("difflib.py", "heapq.py"):                                  # it, or one it imports, left by a call
        (tmp_path / planted).write_text("x")
        assert _bash_why(tmp_path, cmd), planted
        (tmp_path / planted).unlink()
