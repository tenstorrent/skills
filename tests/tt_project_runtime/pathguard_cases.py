# Credential folder write guard tests (pathguard.py, through hook.py's PreToolUse and a full-tools
# coordinator's) of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file. Every test runs under a
# scratch HOME, XDG_CONFIG_HOME and GH_CONFIG_DIR and only asks the guard: nothing writes a real login.
from __future__ import annotations


@pytest.fixture
def cred_home(tmp_path, monkeypatch):
    """A scratch home with ~/.ssh and gh's dir, a work dir holding a symlink into ~/.ssh, an account
    settings dir, and a run dir (the hook acts only in a run)."""
    from types import SimpleNamespace
    from ttp import project
    home, work, run = tmp_path / "h", tmp_path / "w", tmp_path / "run"
    for d in (home / ".ssh", home / ".config" / "gh", work, run, tmp_path / "acct"):
        d.mkdir(parents=True)
    (work / "link").symlink_to(home / ".ssh" / "authorized_keys")
    for var, val in (("HOME", home), ("XDG_CONFIG_HOME", home / ".config"), ("GH_CONFIG_DIR", home / ".config" / "gh"),
                     ("TTP_RUN_DIR", run)):
        monkeypatch.setenv(var, str(val))
    monkeypatch.delenv("TTP_PROJECT", raising=False)
    monkeypatch.setattr(project, "HOME_DIR", tmp_path / "acct")
    return SimpleNamespace(home=home, work=work, run=run, acct=tmp_path / "acct")


def _pg_bash(cmd, cwd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd)}


PG_SHELL_DENIED = [
    "echo x > ~/.ssh/config", 'echo x >> "$HOME/.config/gh/hosts.yml"', "printf x &> ${HOME}/.ssh/config",
    "echo x | tee -a ${HOME}/.ssh/authorized_keys", "cp /tmp/x ~/.ssh/", "cp -t ~/.ssh a b", "mv ~/.ssh/id_rsa /tmp/",
    "install -m600 a $GH_CONFIG_DIR/hosts.yml", "rsync -a src/ ~/.ssh/", "ln -sf /tmp/x ~/.ssh/config",
    "sed -i 's/a/b/' $XDG_CONFIG_HOME/gh/hosts.yml", "sed -Ei 's/a/b/' ~/.ssh/config", "perl -i -pe 's/a/b/' ~/.ssh/config",
    "chmod 600 ~/.ssh/id_rsa", "chown me ~/.ssh", "chmod -R 777 ~/.config", "rm -rf ~/.config", "rm ~/.netrc",
    "unlink ~/.config/gh/hosts.yml", "truncate -s0 ~/.git-credentials", "touch ~/.aws/credentials", "mkdir ~/.ssh/new",
    "dd if=/dev/zero of=~/.ssh/id_rsa", "curl -o ~/.ssh/k https://example.com/k", "wget -O ~/.kube/config https://x",
    "echo x > link", "timeout 5 env FOO=1 rm ~/.ssh/known_hosts", "bash -c 'echo y > ~/.ssh/config'",
    "sh -c \"cp a $HOME/.docker/config.json\"", "cat <<EOF > ~/.ssh/config\nHost x\nEOF",
    "python3 -c \"open('$HOME/.ssh/config','w').write('x')\"",
    "python3 - <<'PY'\nfrom pathlib import Path\n(Path.home()/'.config'/'gh'/'hosts.yml').write_text('token: x')\nPY",
    "python3 -c \"import shutil; shutil.rmtree('~/.ssh')\"",
    "node -e \"require('fs').writeFileSync(process.env.HOME+'/.ssh/x','')\"",
    "ruby -e 'File.write(Dir.home + \"/.ssh/x\", \"\")'", "perl -e 'open(F, \">$ENV{HOME}/.netrc\")'",
    "gh auth login --with-token < t", "gh auth logout", "gh config set editor vim", "docker login -u x",
    "aws configure set region x", "kubectl config use-context x", "git credential approve < f", "ssh-keygen -R host",
    "ssh-keygen -t ed25519 -f ~/.ssh/id_new -N ''", "ssh-keygen -t ed25519", "touch ~/.config/example-access/f",
    "ssh box true > ~/.ssh/known_hosts", "bash -c \"ssh box 'cat k' >> ~/.ssh/authorized_keys\"",
    "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: ~/.config/gh/hosts.yml\n+x\n*** End Patch\nEOF",
    'eval "echo x > ~/.ssh/config"', "eval 'cp a' \"$HOME/.ssh/\"", "bash -c 'eval \"echo x >> ~/.netrc\"'",
    "python3 -c \"import os; os.system('echo x > ~/.ssh/config')\"",
    "python3 -c \"import subprocess; subprocess.run(['tee', os.path.expanduser('~/.ssh/config')])\"",
    "python3 -c \"import subprocess as s; s.run(['gh', 'auth', 'login', '--with-token'])\"",
    "python3 -c \"import subprocess; subprocess.run('echo x >' + ' ~/.ssh/config', shell=True)\"",
    "python3 -c \"import subprocess; subprocess.run(['bash', '-c', 'rm -f ~/.ssh/known_hosts'])\"",
    "python3 -c \"import os; p = os.path.expanduser('~/.ssh/config')\nopen(p, 'w')\"",
    "perl -e 'system(\"truncate\", \"-s0\", \"$ENV{HOME}/.git-credentials\")'", "ruby -e '`touch ~/.ssh/x`'",
    'builtin eval "echo x > ~/.ssh/config"', "eval echo x \\> ~/.ssh/config",
    "perl -e 'open(my $f, \">>\", \"$ENV{HOME}/.ssh/config\")'",
    # a write command whose target the code builds
    "perl -e 'system(\"tee $ENV{HOME}/.ssh/config\")'",
    "python3 -c \"import os; p=os.path.expanduser('~/.ssh/config'); os.system('tee ' + p + ' < /tmp/x')\"",
    "python3 -c \"import subprocess,os; p=os.path.expanduser('~/.ssh/config'); subprocess.run(f'echo x > {p}', shell=True)\"",
    "python3 -c \"import subprocess,os; p=os.path.expanduser('~/.ssh/config'); subprocess.run('dd if=/tmp/x of=' + p, shell=True)\"",
    "python3 -c \"import os; p=os.path.expanduser('~/.ssh/config'); os.system('sed -i s/a/b/ ' + p)\"",
    "python3 -c \"import os; os.system('rm -f %s' % os.path.expanduser('~/.ssh/known_hosts'))\"",
    "python3 -c \"import os; os.system('echo x | tee ' + os.path.expanduser('~/.ssh/config'))\"",
    "node -e \"require('child_process').execSync('tee ' + require('os').homedir() + '/.ssh/config')\"",
    "python3 -c \"import subprocess,os; p=os.path.expanduser('~/.ssh/id_new'); subprocess.run(['ssh-keygen','-t','ed25519','-f',p,'-N',''])\"",
    "python3 -c \"import os; p=os.path.expanduser('~/.ssh/c'); os.system('cat /tmp/x > %s' % p)\"",
    "python3 -c \"import os; p=os.path.expanduser('~/.ssh/c'); os.system('bash -c \\\"tee ' + p + '\\\"')\"",
    "node -e \"const p=require('os').homedir()+'/.ssh/c'; require('child_process').execSync(`cat /tmp/x > ${p}`)\"",
    "python3 -c \"import os; p=os.path.expanduser('~/.ssh/c'); os.system('echo x >& ' + p)\"",
    # a write next to an ssh or scp word: the key path in a write position, or an ssh -i read beside a write
    "python3 -c \"import subprocess,os; subprocess.run(['tee','-i',os.path.expanduser('~/.ssh/k')])\"",
    "python3 -c \"import subprocess,os; subprocess.run(['cp','/tmp/x','-i',os.path.expanduser('~/.ssh/k')])\"",
    "python3 -c \"import os; os.system('X=ssh tee -i ' + os.path.expanduser('~/.ssh/k'))\"",
    "python3 -c \"import os; os.system(\\\"X='ssh -i' tee \\\" + os.path.expanduser('~/.ssh/k'))\"",
    "python3 -c \"import subprocess,os; subprocess.run(['scp','-r','box:/a','-i',os.path.expanduser('~/.ssh/k')])\"",
    "python3 -c \"import subprocess,os; subprocess.run(['scp','-i',os.path.expanduser('~/.ssh/k'),'box:/a',os.path.expanduser('~/.ssh/k2')])\"",
    "python3 -c \"import subprocess,os; subprocess.run(['ssh','-i',os.path.expanduser('~/.ssh/k'),'-E',os.path.expanduser('~/.ssh/k'),'box','mkdir x'])\"",
    "python3 -c \"import subprocess,os; open(subprocess.run(['ssh','-i',os.path.expanduser('~/.ssh/k'),'box','true']).args[2],'w')\"",
    "python3 -c \"import subprocess,os; a=['ssh','-i',os.path.expanduser('~/.ssh/k')]; subprocess.run(['tee', a[2]])\"",
    "python3 -c \"import subprocess,os; subprocess.run(['ssh','-i',(k:=os.path.expanduser('~/.ssh/k')),'box','true']); subprocess.run(['tee',k])\"",
    "python3 -c \"import subprocess,os; subprocess.run(['ssh','-i',os.system('touch '+os.path.expanduser('~/.ssh/x')) or 'k','box','true'])\"",
    "python3 -c \"import subprocess,os; subprocess.run(['ssh','-i',os.path.expanduser('~/.ssh/k'),'box','true']); subprocess.run(['mkdir',os.path.expanduser('~/.ssh/d')])\"",
    "python3 -c \"import os; os.system('ssh -i ' + os.path.expanduser('~/.ssh/k') + ' box true > ' + os.path.expanduser('~/.ssh/k'))\"",
    "python3 -c \"import os; os.system('ssh -i ' + os.path.expanduser('~/.ssh/k') + '; tee \\\"$_\\\"')\"",
    "python3 -c \"import os; os.system(f'ssh -i {os.system(\\\"tee \\\" + os.path.expanduser(\\\"~/.ssh/k\\\"))} box')\"",
    "python3 -c \"import subprocess,os; subprocess.run('tee \\\"${GIT_SSH_COMMAND#ssh -i }\\\"', shell=True, env=dict(os.environ, GIT_SSH_COMMAND='ssh -i '+os.path.expanduser('~/.ssh/k')))\"",
    "perl -e 'system(\"ssh\", \"-i\", \"$ENV{HOME}/.ssh/k\", \"box\", \"true\"); system(\"tee\", \"$ENV{HOME}/.ssh/k\");'",
    # a run call the code defines or rebinds itself, or one whose module it replaces
    'python3 -c "import shutil,os; run=lambda *a: shutil.copy(\'/tmp/x\', a[2]); run(\'ssh\',\'-i\',os.path.expanduser(\'~/.ssh/k\'))"',
    'node -e "const fs=require(\'fs\'); const run=(...a)=>fs.writeFileSync(a[2],\'x\'); run(\'ssh\',\'-i\',require(\'os\').homedir()+\'/.ssh/k\')"',
    'python3 -c "import subprocess,os; subprocess=type(\'S\',(),{\'run\':staticmethod(lambda a: open(a[2],\'w\'))}); subprocess.run([\'ssh\',\'-i\',os.path.expanduser(\'~/.ssh/k\')])"',
    'python3 -c "import os,shutil; os.system=lambda c: shutil.copy(\'/tmp/x\', c.split()[2]); os.system(\'ssh -i \' + os.path.expanduser(\'~/.ssh/k\'))"',
    'perl -e \'use File::Copy; BEGIN { *CORE::GLOBAL::system = sub { copy("/tmp/x", $_[2]) } } system("ssh", "-i", "$ENV{HOME}/.ssh/k");\'',
    "python3 - <<'PY'\nimport shutil, os\ndef run(*a): shutil.copy('/tmp/x', a[2])\nrun('ssh', '-i', os.path.expanduser('~/.ssh/k'))\nPY",
    "python3 - <<'PY'\nfrom subprocess import run\nimport os, shutil\ndef f(run):\n    run(['ssh', '-i', os.path.expanduser('~/.ssh/k'), 'box', 'mkdir -p /tmp/x'])\nf(lambda a: shutil.copy('/tmp/x', a[2]))\nPY",
    # a GIT_SSH_COMMAND key handed to a program other than git, or to a git or shell that runs other commands
    'python3 -c "import os; os.system(\\"GIT_SSH_COMMAND=\'ssh -i \\" + os.path.expanduser(\'~/.ssh/k\') + \\"\' env | cut -c24- | xargs touch\\")"',
    'python3 -c "import subprocess; subprocess.run(\\"GIT_SSH_COMMAND=\'ssh -i ~/.ssh/k\' git rebase -x ./w.sh main && cp a /tmp/r\\", shell=True)"',
    'python3 -c "import subprocess,os; subprocess.run([\'sh\',\'-c\',\'cp /tmp/x \\"$(env | sed -n s/^GIT_SSH_C[A-Z]*=ssh.-i.//p)\\"\'], env=dict(os.environ, GIT_SSH_COMMAND=\'ssh -i \'+os.path.expanduser(\'~/.ssh/k\')))"',
    'python3 -c "import subprocess,os; subprocess.run([\'python3\',\'-c\',\'import os,shutil; [shutil.copy(\\"/tmp/x\\", v[7:]) for k,v in os.environ.items() if k.startswith(\\"GIT_SSH\\")]\'], env=dict(os.environ, GIT_SSH_COMMAND=\'ssh -i \'+os.path.expanduser(\'~/.ssh/k\')))"',
    'python3 -c "import subprocess,os; subprocess.run([\'bash\',\'-c\',\'cp /tmp/x \\"$(printenv | grep -o \\"/[^ ]*/k$\\")\\"\'], env=dict(os.environ, GIT_SSH_COMMAND=\'ssh -i \'+os.path.expanduser(\'~/.ssh/k\')))"',
    'python3 -c "import subprocess,os; subprocess.run([\'git\',\'-c\',\'alias.x=!cp /tmp/x \\"$(printenv | grep -o \\"/[^ ]*/k$\\")\\"\',\'x\'], env=dict(os.environ, GIT_SSH_COMMAND=\'ssh -i \'+os.path.expanduser(\'~/.ssh/k\')))"',
    'python3 -c "import subprocess,os; subprocess.run(\'git fetch; printenv | grep -o \\"/[^ ]*/k$\\" | xargs touch\', shell=True, env=dict(os.environ, GIT_SSH_COMMAND=\'ssh -i \'+os.path.expanduser(\'~/.ssh/k\')))"',
    'python3 -c "import os; os.system(\\"GIT_SSH_COMMAND=\'ssh -i ~/.ssh/k\' env | cut -c24- | xargs touch\\")"',
    # inline interpreter code that uses a key as an ssh or git identity beside a write word: no exemption
    "python3 -c \"import subprocess,os; subprocess.run(['ssh','-i',os.path.expanduser('~/.ssh/k'),'box','mkdir -p /tmp/x'])\"",
    "python3 -c \"import subprocess,os; subprocess.run(['git','fetch'], env=dict(os.environ, GIT_SSH_COMMAND='ssh -i '+os.path.expanduser('~/.ssh/k')))\"",
    "python3 -c \"import subprocess,os; subprocess.run(['scp','-i',os.path.expanduser('~/.ssh/k'),'box:/tmp/a','/tmp/b'], check=True)\"",
    "python3 -c \"import os; os.system('ssh -i ' + os.path.expanduser('~/.ssh/k') + ' box mkdir -p /tmp/x')\"",
    "python3 -c \"import os; os.system(f'ssh -i {os.path.expanduser(\\\"~/.ssh/k\\\")} box rm -rf /tmp/x')\"",
    "python3 -c \"import subprocess; subprocess.run(\\\"GIT_SSH_COMMAND='ssh -i ~/.ssh/k' git clone box:r /tmp/r && cp a /tmp/r\\\", shell=True)\"",
    "python3 -c \"import subprocess,os; subprocess.run(['ssh','-o','BatchMode=yes','-p','22','-i',os.path.expanduser('~/.ssh/k'),'-F',os.path.join(os.environ['HOME'],'.ssh','config'),'box','touch /tmp/x'])\"",
    "perl -e 'system(\"ssh\", \"-i\", \"$ENV{HOME}/.ssh/k\", \"box\", \"mkdir -p /tmp/x\");'",
    "python3 - <<'PY'\nimport subprocess, os\nsubprocess.run(['ssh', '-i', os.path.expanduser('~/.ssh/k'), 'box', 'mkdir -p /tmp/x'])\nPY",
    "python3 - <<'PY'\nfrom subprocess import run\nimport os\nrun(['ssh', '-i', os.path.expanduser('~/.ssh/k'), 'box', 'mkdir -p /tmp/x'])\nPY",
    # code that rebinds its runner, or has git run its config, editor or a hook it wrote (review #6780)
    'python3 -c "import subprocess,os,shutil; subprocess.Popen=lambda a,**k: shutil.copy(\'/tmp/x\',a[2]); subprocess.run([\'ssh\',\'-i\',os.path.expanduser(\'~/.ssh/k\')])"',
    'python3 -c "import subprocess,os,shutil; subprocess.run.__code__=(lambda *a,**k: shutil.copy(\'/tmp/x\',a[0][2])).__code__; subprocess.run([\'ssh\',\'-i\',os.path.expanduser(\'~/.ssh/k\')])"',
    'python3 -c "import subprocess,os,shutil; exec(\'subprocess.run=lambda a: shutil.copy(1,a[2])\'); subprocess.run([\'ssh\',\'-i\',os.path.expanduser(\'~/.ssh/k\')])"',
    "python3 - <<'PY'\nimport subprocess, os, shutil\nfor subprocess.run in [lambda a: shutil.copy('/tmp/x', a[2])]: pass\nsubprocess.run(['ssh', '-i', os.path.expanduser('~/.ssh/k')])\nPY",
    "python3 - <<'PY'\nimport subprocess, os, shutil\nsubprocess.run, x = (lambda a: shutil.copy('/tmp/x', a[2])), 1\nsubprocess.run(['ssh', '-i', os.path.expanduser('~/.ssh/k')])\nPY",
    "python3 - <<'PY'\nimport subprocess, os, shutil\nsubprocess.run: object = lambda a: shutil.copy('/tmp/x', a[2])\nsubprocess.run(['ssh', '-i', os.path.expanduser('~/.ssh/k')])\nPY",
    'python3 -c "import subprocess,os; open(\'.git/hooks/pre-commit\',\'w\').write(\'#!/bin/sh\\ncp /tmp/x \\"\\$(env | sed -n s/^GIT_SSH_C[A-Z]*=ssh.-i.//p)\\"\'); os.chmod(\'.git/hooks/pre-commit\',0o755); subprocess.run([\'git\',\'commit\',\'--allow-empty\',\'-m\',\'x\'], env=dict(os.environ, GIT_SSH_COMMAND=\'ssh -i \'+os.path.expanduser(\'~/.ssh/k\')))"',
    'python3 -c "import subprocess,os; subprocess.run([\'git\',\'x\'], env=dict(os.environ, GIT_CONFIG_COUNT=\'1\', GIT_CONFIG_KEY_0=\'alias.x\', GIT_CONFIG_VALUE_0=\'!cp /tmp/x \\"\\$(env | sed -n s/^GIT_SSH_C[A-Z]*=ssh.-i.//p)\\"\', GIT_SSH_COMMAND=\'ssh -i \'+os.path.expanduser(\'~/.ssh/k\')))"',
    'python3 -c "import subprocess,os; subprocess.run([\'git\',\'commit\',\'--allow-empty\'], env=dict(os.environ, GIT_EDITOR=\'cp /tmp/x \\"\\$(env | sed -n s/^GIT_SSH_C[A-Z]*=ssh.-i.//p)\\" #\', GIT_SSH_COMMAND=\'ssh -i \'+os.path.expanduser(\'~/.ssh/k\')))"',
    # a plain shell key use beside anything that may run a local program or write
    "ssh -i ~/.ssh/k host 'touch x'; touch /tmp/y",
    "GIT_SSH_COMMAND='ssh -i ~/.ssh/k' git clone box:r /tmp/r && cp a /tmp/r",
    "ssh -i ~/.ssh/k -o ProxyCommand='cp /tmp/x ~/.ssh/k' host true",
    'ssh -i ~/.ssh/k -oLocalCommand=x -o PermitLocalCommand=yes host true',
    'ssh -F /tmp/cfg -i ~/.ssh/k host true',
    "GIT_EDITOR='cp /tmp/x ~/.ssh/k #' GIT_SSH_COMMAND='ssh -i ~/.ssh/k' git commit",
    "GIT_EDITOR=vi GIT_SSH_COMMAND='ssh -i ~/.ssh/k' git fetch",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.x GIT_CONFIG_VALUE_0='!cp /tmp/x ~/.ssh/k' GIT_SSH_COMMAND='ssh -i ~/.ssh/k' git x",
    "GIT_SSH_COMMAND='ssh -i ~/.ssh/k' git -c alias.x='!cp /tmp/x ~/.ssh/k' x",
    "GIT_SSH_COMMAND='ssh -i ~/.ssh/k' git clone -c core.hooksPath=/tmp/h box:r /tmp/r",
    "GIT_SSH_COMMAND='ssh -i ~/.ssh/k' git clone --template=/tmp/t box:r /tmp/r",
    "GIT_SSH_COMMAND='ssh -i ~/.ssh/k; cp /tmp/x ~/.ssh/k' git fetch",
    "GIT_SSH_COMMAND='ssh -i ~/.ssh/k -o ProxyCommand=sh' git fetch",
    "GIT_SSH_COMMAND='ssh -i ~/.ssh/k' git push",
    "GIT_SSH_COMMAND='ssh -i ~/.ssh/k' env | cut -c24- | xargs touch",
    "export GIT_SSH_COMMAND='ssh -i ~/.ssh/k'; git fetch",
    'ssh -i ~/.ssh/k host "echo $(cat /tmp/x > ~/.ssh/k)"',
    'ssh host -i ~/.ssh/k; cat /tmp/x > "$_"',
    'ssh -i ~/.ssh/k host true | tee /tmp/o',
    'ssh -i ~/.ssh/k -E ~/.ssh/log host true',
    'xargs ssh -i ~/.ssh/k < /tmp/args',
    'bash -c "ssh -i ~/.ssh/k -o ProxyCommand=\'cp /tmp/x ~/.ssh/k\' h"',
    'X=1 ssh -i ~/.ssh/k host true',
    'ssh -i ~/.ssh/k host true >& "$o"',
    'ssh host -i ~/.ssh/k > "$_"',
    'ssh -i ~/.ssh/k host true & cp /tmp/x "$_"',
    'ssh -i ~/.ssh/k host true\ncp /tmp/x "$_"',
    'ssh -vi ~/.ssh/k -oKnownHostsCommand=sh h',
    "GIT_SSH_COMMAND='ssh -i ~/.ssh/k' command git fetch",
    "GIT_SSH_COMMAND='ssh -i ~/.ssh/k' git fetch ext::sh",
    "GIT_SSH_COMMAND='ssh -i ~/.ssh/k' git clone box:r ~/.ssh",
    "GIT_SSH_COMMAND='ssh -i ~/.ssh/k' git clone box:r /tmp/r --upload-pack=sh",
    'ssh -i ~/.ssh/k h `cp /tmp/x ~/.ssh/k`',
    'ssh host -o ProxyCommand=sh -i ~/.ssh/k true',
    'ssh -E ~/.ssh/log host true',
]
PG_SHELL_ALLOWED = [
    "cat ~/.ssh/config", "ls -la ~/.ssh", "ssh -i ~/.ssh/id_rsa host true", "cp ~/.ssh/id_rsa.pub /tmp/pub",
    "grep Host ~/.ssh/config > /tmp/hosts", "ssh-keygen -lf ~/.ssh/id_rsa.pub", "ssh-keygen -y -f ~/.ssh/id_rsa",
    "gh auth status", "gh pr list", "rm -rf build ~/.cache/x", "python3 -c \"print(open('$HOME/.ssh/config').read())\"",
    "sed -n 1p ~/.ssh/config", "echo hi > out.txt 2>&1", "echo 'a > ~/.ssh/config is refused'",
    "GH_CONFIG_DIR=/tmp/scratch gh auth login --with-token < t", "ssh-keygen -t ed25519 -f /tmp/k -N ''",
    "python3 -m pytest -q tests/x.py 2>&1 | tail -5", "mkdir -p /tmp/a && echo x > /tmp/a/b",
    "H=$(mktemp -d); mkdir -p \"$H/.ssh\"; echo x > \"$H/.ssh/config\"", "kubectl config view", "aws configure list",
    "rsync -e 'ssh -i ~/.ssh/k' src host:dst", "chmod 700 ~",
    "ssh box 'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys' < k.pub", "git commit -qm 'docs: > ~/.netrc example'",
    "python3 -c \"import subprocess; subprocess.run(['ssh','-F','~/.ssh/config','box'])\"",
    "python3 -c \"print(1>0); import os; print(os.listdir(os.path.expanduser('~/.ssh')))\"",
    "eval \"cat ~/.ssh/config > /tmp/c\"", "python3 -c \"import os; os.system('ssh -i ~/.ssh/k box true > /tmp/o')\"",
    "python3 -c \"import subprocess,os; subprocess.run(['ssh','-i',os.path.expanduser('~/.ssh/id_rsa'),'box','true'])\"",
    "python3 -c \"import os; print(len(os.listdir(os.path.expanduser('~/.ssh'))) > 0)\"",
    "python3 -c \"import subprocess,os; subprocess.run(['ssh-keygen','-lf',os.path.expanduser('~/.ssh/id_rsa.pub')])\"",
    "python3 -c \"import os; p=os.path.expanduser('~/.ssh/config'); os.system('cat ' + p + ' 2>&1')\"",
    "python3 -c \"import os; p=os.path.expanduser('~/.ssh/config'); os.system('grep Host ' + p + ' > /tmp/h')\"",
    # a plain shell key use: ssh, scp or sftp -i KEY (its remote command writes on the host), or
    # GIT_SSH_COMMAND='ssh -i KEY' git fetch|clone|ls-remote
    "ssh -i ~/.ssh/k host 'echo hi > /tmp/x'",
    'ssh -i ~/.ssh/k host mkdir -p /tmp/x',
    "ssh -i ~/.ssh/k -p 22 -o BatchMode=yes host 'rm -rf /tmp/x' 2>&1",
    'timeout 30 ssh -i ~/.ssh/k host true >/dev/null && echo ok',
    'scp -i ~/.ssh/k box:/tmp/a /tmp/b',
    'sftp -i ~/.ssh/k box',
    'ssh -i ~/.ssh/id_rsa host true',
    "ssh box 'cat >> ~/.ssh/authorized_keys' < /tmp/pub",
    "GIT_SSH_COMMAND='ssh -i ~/.ssh/k' git clone box:r /tmp/r",
    "GIT_SSH_COMMAND='ssh -i ~/.ssh/k' git fetch",
    'GIT_SSH_COMMAND="ssh -i $HOME/.ssh/k" git ls-remote box:r',
    "cd /tmp/r && GIT_SSH_COMMAND='ssh -i ~/.ssh/k -o IdentitiesOnly=yes' git fetch origin",
    'scp -i ~/.ssh/k -P 2222 box:/tmp/a /tmp/b',
    'ssh -i ~/.ssh/k host ls -la ~/.ssh',
    'ssh -i ~/.ssh/k host true > /tmp/out 2>&1',
]


@pytest.mark.parametrize("cmd", PG_SHELL_DENIED)
def test_pathguard_denies_shell_writes_to_credential_folders(cred_home, cmd):
    from ttp import hook
    cfg = {"guards": {"protected_paths": ["~/.config/example-access"]}}
    assert pathguard_why(_pg_bash(cmd, cred_home.work), cfg), cmd
    if "example-access" not in cmd:   # the hook asks it before its other checks
        out, _ = hook.pre_tool_use(_pg_bash(cmd, cred_home.work))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "credential folders" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("cmd", PG_SHELL_ALLOWED)
def test_pathguard_lets_reads_and_scratch_writes_pass(cred_home, cmd):
    assert pathguard_why(_pg_bash(cmd, cred_home.work)) is None, cmd


def test_pathguard_names_the_shell_route_for_a_key_in_inline_code(cred_home):
    why = pathguard_why(_pg_bash(next(c for c in PG_SHELL_DENIED if "'git','fetch'" in c), cred_home.work))
    assert "as a plain shell command" in why and "IdentityFile" in why
    assert "IdentityFile" not in pathguard_why(_pg_bash("echo x > ~/.ssh/config", cred_home.work))


def test_pathguard_lets_ssh_read_its_own_config_and_host_lists_by_option(cred_home):
    """-F, -o UserKnownHostsFile= and GlobalKnownHostsFile= naming a settings file in ~/.ssh are ssh's own reads,
    beside a key too (credguard.ssh_files); a key given as one, or a write beside it, is still refused."""
    ssh = cred_home.home / ".ssh"
    (ssh / "site.conf").write_text("Host box\n")
    (ssh / "site-hosts").write_text("box ssh-ed25519 AAAA\n")
    (ssh / "k").write_text("-----BEGIN OPENSSH " + "PRIVATE KEY-----\n")   # split so this file holds no key header
    for cmd in ("ssh -F ~/.ssh/site.conf box true", "ssh -F ~/.ssh/site.conf -i ~/.ssh/k box true",
                "ssh -o UserKnownHostsFile=~/.ssh/site-hosts -o GlobalKnownHostsFile=~/.ssh/site-hosts box",
                "scp -F ~/.ssh/site.conf -i ~/.ssh/k a box:", "rsync -e 'ssh -F ~/.ssh/site.conf -i ~/.ssh/k' a box:",
                "GIT_SSH_COMMAND='ssh -F ~/.ssh/site.conf -i ~/.ssh/k' git fetch",
                "git -c core.sshCommand='ssh -F ~/.ssh/site.conf' fetch"):
        assert pathguard_why(_pg_bash(cmd, cred_home.work)) is None, cmd
    for cmd in ("ssh -F ~/.ssh/k box", "ssh -o UserKnownHostsFile=~/.ssh/k box",
                "ssh -F ~/.ssh/site.conf box true > ~/.ssh/site.conf", "ssh -F ~/.ssh/site.conf box; cp a ~/.ssh/site.conf",
                "ssh -F ~/.ssh/site.conf -o ProxyCommand=true -i ~/.ssh/k box"):
        assert pathguard_why(_pg_bash(cmd, cred_home.work)), cmd


def pathguard_why(payload, cfg=None):
    from ttp import pathguard
    return pathguard.refusal(payload, cfg=cfg or {})


def test_pathguard_file_patch_and_mcp_tools(cred_home):
    h, w = cred_home.home, str(cred_home.work)
    denied = [("Write", {"file_path": "~/.ssh/config", "content": "x"}), ("Edit", {"file_path": str(h / ".config/gh/hosts.yml")}),
              ("MultiEdit", {"file_path": "$HOME/.netrc"}), ("NotebookEdit", {"notebook_path": "link"}),
              ("apply_patch", {"input": f"*** Begin Patch\n*** Update File: {h}/.ssh/config\n@@\n-a\n+b\n*** End Patch"}),
              ("apply_patch", {"input": "*** Begin Patch\n*** Update File: notes.txt\n*** Move to: ~/.ssh/notes\n*** End Patch"}),
              ("apply_patch", {"input": "*** Begin Patch\n*** Delete File: ~/.git-credentials\n*** End Patch"}),
              ("mcp__fs__write_file", {"path": "~/.ssh/config", "content": "x"}),
              ("mcp__fs__move_file", {"source": "~/.config", "destination": "/tmp/c"})]
    allowed = [("Read", {"file_path": "~/.ssh/config"}), ("Write", {"file_path": "new.txt"}),
               ("mcp__fs__read_file", {"path": "~/.ssh/config"}), ("mcp__fs__write_file", {"path": "/tmp/x"}),
               ("apply_patch", {"input": "*** Begin Patch\n*** Add File: src/a.py\n+x\n*** End Patch"})]
    for tool, inp in denied:
        assert pathguard_why({"tool_name": tool, "tool_input": inp, "cwd": w}), (tool, inp)
    for tool, inp in allowed:
        assert pathguard_why({"tool_name": tool, "tool_input": inp, "cwd": w}) is None, (tool, inp)


def test_pathguard_script_files_a_command_runs(cred_home):
    w = cred_home.work
    (w / "fix.sh").write_text("#!/bin/sh\necho 'token: x' > \"$HOME/.config/gh/hosts.yml\"\n")
    (w / "fix.py").write_text("import os\nopen(os.path.expanduser('~/.ssh/config'), 'a').write('x')\n")
    (w / "ok.py").write_text("print(open('/etc/hostname').read())\n")
    for cmd in ("bash fix.sh", "sh ./fix.sh", "python3 fix.py"):
        assert pathguard_why(_pg_bash(cmd, w)), cmd
    assert pathguard_why(_pg_bash("python3 ok.py", w)) is None
    # The plugin's own shipped scripts (setup-box writes ~/.ssh on a remote box, in ssh text) are not read
    from ttp import pathguard
    shipped = os.path.join(pathguard.PLUGIN_ROOT, "skills", "setup-box", "scripts", "box_account.py")
    assert os.path.exists(shipped)
    assert pathguard_why(_pg_bash(f"python3 {shipped} keys box --run", w)) is None


PG_DATA = "~/data/records"
PG_DATA_FILE = "os.path.expanduser('~/data/records/a.json')"
PG_DATA_WRITES = [   # each Python write form, its target in a protected_paths folder
    f"python3 -c \"import os; p = {PG_DATA_FILE}\nwith open(p, 'w') as f: f.write('x')\"",
    f"python3 -c \"import os\ndef put(q): open(q, 'a').write('x')\nput({PG_DATA_FILE})\"",
    "python3 -c \"import os, glob\nfor f in glob.glob(os.path.expanduser('~/data/records/*')): os.remove(f)\"",
    f"python3 -c \"import shutil, os; shutil.copy('/tmp/x', {PG_DATA_FILE})\"",
    "python3 -c \"from pathlib import Path; (Path.home() / 'data' / 'records' / 'a').write_text('x')\"",
    "python3 -c \"from pathlib import Path; p = Path.home() / 'data/records/a'; p.unlink()\"",
    "python3 -c 'import sys, os; os.remove(sys.argv[1])' ~/data/records/a.json",
    "F=~/data/records/a python3 -c 'import os; open(os.environ[\"F\"], \"w\")'",
    f"python3 -c \"from os import remove as r; import os; r({PG_DATA_FILE})\"",
    f"python3 -c \"import os; f = os.remove; f({PG_DATA_FILE})\"",
    f"python3 -c \"import os; list(map(os.remove, [{PG_DATA_FILE}]))\"",
    f"python3 -c \"import os; exec('open(' + repr({PG_DATA_FILE}) + ', \\\"w\\\")')\"",
    f"python3 -c \"import zipfile, os; zipfile.ZipFile({PG_DATA_FILE}, 'w')\"",
    f"python3 -c \"import os; p = {PG_DATA_FILE}; m = 'w'; open(p, m)\"",
    f"python3 -c \"import os; d = {{'k': {PG_DATA_FILE}}}; open(d['k'], 'w')\"",
    f"python3 -c \"import os\nclass W:\n  def __init__(s, p): open(p, 'w')\nW({PG_DATA_FILE})\"",
    f"python3 -c \"import os, subprocess; subprocess.run(['cp', '/tmp/x', {PG_DATA_FILE}])\"",
    f"python3 - <<'PY'\nimport os\nopen({PG_DATA_FILE}, 'w')\nPY",
]
PG_DATA_READS = [   # reads of the folder, and mentions of it only in quoted data
    f"python3 -c \"import os, json\nrows = [json.load(open(f)) for f in [{PG_DATA_FILE}]]\n"
    "json.dump(rows, open('/tmp/out.json', 'w'))\"",
    f"python3 -c \"import os; p = {PG_DATA_FILE}; print(os.path.exists(p)); open('/tmp/o', 'w').write(p)\"",
    "python3 -c 'import sys; print(sys.argv[1])' ~/data/records/a.json",
    "python3 -c 'import json; json.dump({\"artifacts\": [\"~/data/records/a.json\"]}, open(\"/tmp/r.json\", \"w\"))'",
    "python3 -c 'import json, sys; json.dump({\"summary\": \"read ~/data/records\"}, sys.stdout)'",
    "grep -c 'save ~/data/records' /tmp/log; python3 -c 'print(1)'",
    "python3 -c 'print(1)' --note 'wrote a copy into ~/data/records'",
    "python3 -c 'import sys; open(\"/tmp/o\", \"w\").write(sys.argv[1])' '~/data/records is read only'",
    "python3 report.py /tmp/out.html",
]


@pytest.mark.parametrize("cmd", PG_DATA_READS)
def test_pathguard_tells_reads_and_quoted_data_from_writes(cred_home, cmd):
    """A script that reads a protected_paths folder and writes elsewhere passes, and so does a path
    only inside quoted data (a JSON hand-off, a note, another command's argument)."""
    (cred_home.work / "report.py").write_text(
        "import glob, json, os, sys\nrows = []\n"
        "for f in sorted(glob.glob(os.path.expanduser('~/data/records/*.json'))):\n"
        "    with open(f) as fh:\n        rows.append(json.load(fh))\n"
        "out = sys.argv[1] if len(sys.argv) > 1 else 'out.html'\n"
        "with open(out, 'w') as o:\n    o.write(''.join(map(str, rows)))\n")
    assert pathguard_why(_pg_bash(cmd, cred_home.work), {"guards": {"protected_paths": [PG_DATA]}}) is None, cmd


@pytest.mark.parametrize("cmd", PG_DATA_WRITES)
def test_pathguard_still_refuses_each_python_write_form(cred_home, cmd):
    assert pathguard_why(_pg_bash(cmd, cred_home.work), {"guards": {"protected_paths": [PG_DATA]}}), cmd


def test_pathguard_refuses_a_script_that_writes_a_protected_folder_it_reads(cred_home):
    w = cred_home.work
    (w / "sync.py").write_text("import glob, os, shutil\n"
                               "for f in glob.glob(os.path.expanduser('~/data/records/*.json')):\n"
                               "    shutil.copy('/tmp/new.json', f)\n")
    (w / "dyn.py").write_text("import os\np = os.path.expanduser('~/data/records/a')\n"
                              "getattr(os, 'remove')(p)\n")   # code it cannot follow: any write word counts
    cfg = {"guards": {"protected_paths": [PG_DATA]}}
    for cmd in ("python3 sync.py", "python3 dyn.py"):
        assert pathguard_why(_pg_bash(cmd, w), cfg), cmd

PG_FAIL_CLOSED = [   # calls the read pass cannot prove are reads: the word check decides, and refuses
    "python3 -c \"import os; open(os.path.expanduser(b'~/data/records/a'), 'w')\"",
    f"python3 -c \"import os; fd = os.open({PG_DATA_FILE}, 1089); os.write(fd, b'x')\"",
    f"python3 -c \"import os, shelve; shelve.open({PG_DATA_FILE})\"",
    f"python3 -c \"import os, logging; logging.basicConfig(filename={PG_DATA_FILE})\"",
    f"python3 -c \"import os\ndef read(q): return q\nopen(read({PG_DATA_FILE}), 'w')\"",
    f"python3 -c \"import os\nexists = lambda q: q\nopen(exists({PG_DATA_FILE}), 'w')\"",
]


@pytest.mark.parametrize("cmd", PG_FAIL_CLOSED)
def test_pathguard_read_pass_fails_closed(cred_home, cmd):
    assert pathguard_why(_pg_bash(cmd, cred_home.work), {"guards": {"protected_paths": [PG_DATA]}}), cmd


PG_CHDIR = [   # the working directory moves into the folder first: relative writes land there
    "cd {d} && python3 -c \"open('k', 'w')\"",
    "pushd {d} && python3 -c \"open('k', 'w')\"",
    "env -C {d} python3 -c \"open('k', 'w')\"",
    "(cd {d}; python3 -c \"open('k', 'w')\")",
    "python3 -c \"import os; os.chdir(os.path.expanduser('{d}')); open('k', 'w')\"",
    "cd {d} && perl -e 'open(F, \">k\")'",
]


@pytest.mark.parametrize("cmd", PG_CHDIR)
@pytest.mark.parametrize("folder", ["default", "protected_paths"])
def test_pathguard_changed_directory_keeps_the_whole_line_check(cred_home, cmd, folder):
    d, cfg = ("~/." + "ssh", {}) if folder == "default" else (PG_DATA, {"guards": {"protected_paths": [PG_DATA]}})
    assert pathguard_why(_pg_bash(cmd.format(d=d), cred_home.work), cfg), cmd


def test_pathguard_default_folders_get_no_read_relief(cred_home):
    """~/.ssh and the other default folders keep the whole-line word check: a read there with a write
    word elsewhere on the line is refused, as before the read pass."""
    k = "~/." + "ssh/id_x"
    for cmd in (f"python3 -c \"import os; d = open(os.path.expanduser('{k}')).read(); open('out', 'w').write(d)\"",
                f"python3 -c \"import os, json; json.dump(open(os.path.expanduser('{k}')).read(), open('o', 'w'))\""):
        assert pathguard_why(_pg_bash(cmd, cred_home.work), {}), cmd


def test_pathguard_list_adds_account_and_project_paths_never_drops_defaults(cred_home):
    from ttp import pathguard
    (cred_home.acct / "settings.json").write_text(json.dumps({"guards": {"protected_paths": ["~/.config/acct-access"]}}))
    got = pathguard.paths({"guards": {"protected_paths": ["$HOME/.config/example-access", 3, ""]}})
    assert got[:len(pathguard.DEFAULT_PATHS)] == list(pathguard.DEFAULT_PATHS)
    assert "~/.config/acct-access" in got and "$HOME/.config/example-access" in got and 3 not in got
    for cmd in ("touch ~/.config/acct-access/x", "touch ~/.config/example-access/x"):
        assert pathguard_why(_pg_bash(cmd, cred_home.work), {"guards": {"protected_paths": ["~/.config/example-access"]}})
    assert pathguard_why(_pg_bash("touch ~/.config/example-access/x", cred_home.work)) is None   # not listed
    # A project setting of [] keeps the built-in folders
    assert pathguard_why(_pg_bash("touch ~/.ssh/x", cred_home.work), {"guards": {"protected_paths": []}})


def test_pathguard_covers_the_account_home_and_moved_gh_dir(cred_home, monkeypatch):
    """A run under another HOME still may not write the account's own ~/.ssh, nor gh's dir where
    GH_CONFIG_DIR moves it."""
    import pwd
    from ttp import pathguard
    real = pwd.getpwuid(os.getuid()).pw_dir
    assert any(r == os.path.realpath(os.path.join(real, ".ssh")) for r in pathguard.roots(cfg={}))
    assert pathguard_why(_pg_bash(f"echo x > {real}/.ssh/never-written", cred_home.work))
    moved = cred_home.work / "ghdir"
    monkeypatch.setenv("GH_CONFIG_DIR", str(moved))
    assert pathguard_why(_pg_bash(f"echo x > {moved}/hosts.yml", cred_home.work))


def test_pathguard_fails_closed_on_a_parse_error(cred_home, monkeypatch):
    from ttp import pathguard
    monkeypatch.setattr(pathguard, "_refusal", lambda *a: 1 / 0)
    assert pathguard_why(_pg_bash("echo x > ~/.ssh/config", cred_home.work))
    assert pathguard_why(_pg_bash("echo x > out.txt", cred_home.work)) is None


def test_pathguard_full_tools_coordinator(cred_home, monkeypatch, tmp_path):
    """A full-tools coordinator gets the same refusal on Bash, Edit and MCP tools."""
    from ttp import coordtools
    (tmp_path / "coord").mkdir()
    _full_env(tmp_path / "coord", monkeypatch)
    for tool, inp in (("Bash", {"command": "echo x > ~/.ssh/config"}), ("Write", {"file_path": "~/.config/gh/hosts.yml"}),
                      ("mcp__fs__write_file", {"path": "~/.ssh/config"})):
        out, _ = coordtools.pre_tool_use({"tool_name": tool, "tool_input": inp, "cwd": str(cred_home.work)})
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", tool
        assert "credential folders" in out["hookSpecificOutput"]["permissionDecisionReason"]


PG_LOST_KEY = [("Write", {"file_path": "~/.ssh/id_ed25519", "content": "x"}),
               ("Write", {"file_path": "$HOME/.config/gh/hosts.yml", "content": "token: x"}),
               ("Edit", {"file_path": "~/.ssh/id_ed25519", "old_string": "a", "new_string": "b"}),
               ("Edit", {"file_path": "~/.config/gh/hosts.yml", "old_string": "a", "new_string": "b"}),
               ("Bash", {"command": "cat k > ~/.ssh/id_ed25519"}), ("Bash", {"command": "echo t > ~/.config/gh/hosts.yml"}),
               ("Bash", {"command": "cp k ~/.ssh/id_ed25519"}), ("Bash", {"command": "cp k $HOME/.config/gh/hosts.yml"}),
               ("Bash", {"command": "mv k ~/.ssh/id_ed25519.pub"}), ("Bash", {"command": "mv k ~/.config/gh/hosts.yml"})]


@pytest.mark.parametrize("provider", ["claude", "chat"])
def test_pathguard_refuses_the_lost_key_writes_on_hooked_providers(cred_home, provider):
    """The case that lost a key and a gh login: a Write or Edit call, a redirect, cp or mv onto ~/.ssh/id_*
    or gh's hosts.yml. Claude Code and the chat provider (Claude Code on a bridge) run the same hook, whose
    matcher takes these tools; the hook refuses each."""
    from ttp import hook
    from ttp.providers import get_provider
    argv, _ = get_provider(provider).build(role="worker", model="m", effort="high", cwd=str(cred_home.work),
                                           budget_usd=None, read_only=False, schema=None, restrictions={})
    raw = argv[argv.index("--settings") + 1]
    settings = json.loads(raw if raw.lstrip().startswith("{") else open(raw).read())
    matchers = [m["matcher"] for m in settings["hooks"]["PreToolUse"]
                if any(h["command"].endswith("PreToolUse") for h in m["hooks"])]
    for tool, inp in PG_LOST_KEY:
        assert any(m == "*" or re.fullmatch(m, tool) for m in matchers), (provider, tool)   # "*": every tool
        out, _ = hook.pre_tool_use({"tool_name": tool, "tool_input": inp, "cwd": str(cred_home.work)})
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", (provider, tool, inp)
        assert "credential folders" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_pathguard_refuses_the_lost_key_shell_writes_on_codex(cred_home):
    """Codex's hook gets shell calls as exec_command argv: the same shell forms are refused there; apply_patch
    text given through the shell is refused."""
    for tool, inp in PG_LOST_KEY:
        if tool == "Bash":
            assert pathguard_why({"tool_name": "exec_command", "tool_input": {"cmd": ["bash", "-lc", inp["command"]]},
                                  "cwd": str(cred_home.work)}), inp
    assert pathguard_why(_pg_bash("apply_patch '*** Begin Patch\n*** Add File: ~/.ssh/id_ed25519\n+x\n*** End Patch'",
                                  cred_home.work))


def test_pathguard_refuses_codex_native_apply_patch_in_hook_on_runs(cred_home):
    """A hook-on Codex run sends its native patch tool to PreToolUse (codex-cli 0.160, measured: tool_name
    "apply_patch", the patch in tool_input.command, absolute paths) once the matcher takes it. The shipped
    matcher matches it and Bash; the hook refuses a patch into ~/.ssh, gh's dir or a configured path and
    lets one elsewhere through."""
    from ttp import hook
    from ttp.providers.codex import hook_config_args
    args = hook_config_args()
    pre = next(a for a in args if a.startswith("hooks.PreToolUse="))
    matcher = re.search(r'matcher="([^"]*)"', pre).group(1)
    for tool in ("Bash", "apply_patch"):
        assert re.fullmatch(matcher, tool), (matcher, tool)
    (cred_home.acct / "settings.json").write_text(json.dumps({"guards": {"protected_paths": ["~/.config/example-access"]}}))
    h, w = cred_home.home, cred_home.work

    def patch(body):
        return {"tool_name": "apply_patch", "tool_input": {"command": f"*** Begin Patch\n{body}\n*** End Patch"},
                "cwd": str(w)}
    denied = [f"*** Add File: {h}/.ssh/id_ed25519\n+x", f"*** Update File: {h}/.config/gh/hosts.yml\n@@\n-a\n+b",
              f"*** Update File: {w}/notes.txt\n*** Move to: {h}/.ssh/notes", f"*** Delete File: {h}/.ssh/config",
              f"*** Add File: {h}/.config/example-access/token\n+x", "*** Add File: link\n+x",
              f"*** Update File:   {h}/.config/gh/hosts.yml  \n@@\n-a\n+b", f"  *** Add File: {h}/.ssh/k\n+x",
              f"*** Add File: {w}/../h/.ssh/config\n+x", f"*** Add File: {w}/ok.txt\n+ok\n*** Add File: {h}/.ssh/k2\n+y"]
    for body in denied:
        out, _ = hook.pre_tool_use(patch(body))
        assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", body
        assert "credential folders" in out["hookSpecificOutput"]["permissionDecisionReason"]
    for body in (f"*** Add File: {w}/ok.txt\n+ok", f"*** Update File: {w}/src/a.py\n@@\n-a\n+b",
                 f"*** Add File: {h}/.sshx/notes\n+x"):
        out, _ = hook.pre_tool_use(patch(body))
        assert out is None, (body, out)


PG_KEYGEN = "ssh-keygen -q -t ed25519 -N '' -f $HOME/.ssh/id_ed25519"
PG_REMOTE_DENIED = [
    # (a) a file on ssh's stdin, also through a wrapper script
    "ssh host 'bash -s' < fix.sh", "ssh -p 22 -o BatchMode=yes host bash < fix.sh", "ssh host < fix.sh",
    "bash wrap.sh", "ssh host python3 - < fix.py",
    # (b) piped into ssh
    "cat fix.sh | ssh host bash", "cat fix.sh | ssh host 'bash -s'", f'echo "{PG_KEYGEN}" | ssh remote sh',
    # (c) a heredoc
    f"ssh host bash <<'EOF'\nset -e\n{PG_KEYGEN}\nEOF", "ssh remote 'bash -s' <<-EOF\n\tssh-keygen -N '' -f ~/.ssh/id_rsa\n\tEOF",
    # (d) copied, then run there
    "scp fix.sh host: && ssh host bash fix.sh", "rsync -a fix.sh remote:/tmp/ && ssh remote 'bash /tmp/fix.sh'",
    # (e) the remote command itself, its redirects too
    "ssh host 'ssh-keygen -f ~/.ssh/id_ed25519'", "ssh host 'echo x > ~/.ssh/authorized_keys'",
    "ssh host 'ssh-keygen -t ed25519 -N \"\"'", "ssh remote 'rm -rf ~/.ssh'", "ssh host 'cp k /home/u/.ssh/config'",
    "ssh host \"mv k \\$HOME/.ssh/id_rsa.pub\"", "ssh -J jump host 'ssh other \"echo x > ~/.ssh/config\"'",
    "bash -c \"ssh host 'tee ~/.ssh/authorized_keys < k'\"",
]
PG_REMOTE_ALLOWED = [
    "ssh host 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo x > ~/.ssh/known_hosts'",
    "ssh host 'test -e ~/.ssh/purpose_key || ssh-keygen -q -t ed25519 -N \"\" -f ~/.ssh/purpose_key'",
    "ssh host 'bash -s' < ok.sh", "cat ok.sh | ssh host bash", "scp ok.sh host: && ssh host bash ok.sh",
    "ssh host 'bash -s' < missing.sh", "ssh host 'cat ~/.ssh/id_ed25519.pub' > /tmp/pub", "ssh host 'ssh-keygen -R other'",
    "ssh host 'cat >> ~/.ssh/authorized_keys' < k.pub", "ssh host bash <<'EOF'\necho hi > /tmp/out\nEOF",
    "scp fix.sh host: && ssh host 'cat fix.sh'", "cat fix.sh | ssh host 'cat > /tmp/fix.sh'",
]


def _pg_remote_files(w):
    (w / "fix.sh").write_text(f"#!/bin/bash\nset -e\n{PG_KEYGEN}\n")
    (w / "fix.py").write_text("import os\nopen(os.path.expanduser('~/.ssh/authorized_keys'), 'w').write('x')\n")
    (w / "wrap.sh").write_text("#!/bin/bash\nssh host 'bash -s' < fix.sh\n")
    (w / "ok.sh").write_text("#!/bin/bash\nmkdir -p ~/.ssh\ntest -e ~/.ssh/purpose_key || ssh-keygen -f ~/.ssh/purpose_key\n")


@pytest.mark.parametrize("cmd", PG_REMOTE_DENIED)
def test_pathguard_refuses_text_ssh_runs_on_a_host_writing_its_keys(cred_home, cmd):
    """A script or command ssh runs on another host (stdin file, pipe, heredoc, a copy run there, the remote
    command) that writes that host's default keys, ssh config or authorized_keys is refused, ~ read as the
    remote home."""
    from ttp import hook
    _pg_remote_files(cred_home.work)
    why = pathguard_why(_pg_bash(cmd, cred_home.work))
    assert why and "on the remote host" in why, cmd
    out, _ = hook.pre_tool_use(_pg_bash(cmd, cred_home.work))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", cmd


@pytest.mark.parametrize("cmd", PG_REMOTE_ALLOWED)
def test_pathguard_lets_other_remote_writes_and_purpose_keys_pass(cred_home, cmd):
    _pg_remote_files(cred_home.work)
    assert pathguard_why(_pg_bash(cmd, cred_home.work)) is None, cmd


def test_pathguard_local_rules_unchanged_by_the_remote_list(cred_home):
    """Locally all of ~/.ssh stays protected: a purpose key, known_hosts and an appended key are refused."""
    for cmd in ("test -e ~/.ssh/purpose_key || ssh-keygen -f ~/.ssh/purpose_key", "echo x > ~/.ssh/known_hosts",
                "cat k.pub >> ~/.ssh/authorized_keys", "mkdir -p ~/.ssh/sub", "bash ok.sh"):
        _pg_remote_files(cred_home.work)
        why = pathguard_why(_pg_bash(cmd, cred_home.work))
        assert why and "on the remote host" not in why, cmd
    assert pathguard_why(_pg_bash("HOME=/tmp/scratch ssh-keygen -t ed25519", cred_home.work)) is None


def test_pathguard_reads_commands_after_a_comment_line(cred_home):
    """A `#` comment (a script's #! line, a note before a command) ends at its line: the commands after it are
    read, locally and in a script ssh runs."""
    from ttp import hook
    assert hook._segments("#!/bin/sh\nset -e\nrm y # gone\nls") == [["set", "-e"], ["rm", "y"], ["ls"]]
    w = cred_home.work
    (w / "gen.sh").write_text("#!/bin/sh\n# make a key\nssh-keygen -q -N '' -f \"$HOME/.ssh/id_rsa\"\n")
    for cmd in ("bash gen.sh", "# a note\nssh-keygen -f ~/.ssh/id_rsa", "ssh host 'bash -s' < gen.sh"):
        assert pathguard_why(_pg_bash(cmd, w)), cmd
