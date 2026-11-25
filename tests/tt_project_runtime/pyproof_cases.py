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
    # found in review: modules under other names, frame objects, rebound literal names, keyword modes, listings
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
    hops = "; ".join(f"cd a{i % 2}" for i in range(40))                         # each relative cd doubles the folders
    t = time.perf_counter()
    cwds = credguard._cwds(hook._segments(f"{hops}; cat x", strip=False), str(tmp_path), str(tmp_path / "home"))
    assert time.perf_counter() - t < 0.2 and str(tmp_path / "home") in cwds[1:]  # past the cap, read from HOME too
    assert pyproof.rewrite(f"{hops}; python3 -c 'print(1)'", str(tmp_path)) is None
