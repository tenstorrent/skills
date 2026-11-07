# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Credential stores a run never reads, and GitHub writes that skip `git push`.

Runs are unsandboxed: they can read all of HOME and reach any host. A credential a tool call prints
stays in the run's log and transcript, so the stores are kept out of tool calls model-free:
- the daemon gives Claude Code runs a Read deny rule for each file credential_files() names (gh's
  hosts file, git's and netrc's credential files, the agents' own login files, the token and key
  files of aws, gcloud and azure (DIR_STORES), docker's and kubectl's config, ~/.pypirc, and every
  file in ~/.ssh but the known public ones, see ssh_public);
- the worker hook refuses a Read, Grep or Glob call that reaches one, also by a symlink, inside one
  of the folders it names, or one made later in ~/.ssh, and a Grep or Glob over a folder holding one
  (tool_refusal);
- the worker hook (hook.py) refuses a Bash call that names one by path (bash_refusal), or prints a
  token (`gh auth token`, `gh auth status --show-token`, `git credential fill`). ssh, git and gh
  read them themselves. A key path passes in one class of use only (ssh_keys): the direct value of
  `-i` or `-o IdentityFile=` of ssh, scp or sftp, also inside a plain `ssh -i <key>` that git
  (GIT_SSH_COMMAND right before git, `git -c core.sshCommand=`, for fetch, clone and ls-remote) or
  rsync (`-e`, `--rsh`, RSYNC_RSH right before rsync) runs, and only when nothing in that call runs a
  local program: no ProxyCommand, LocalCommand, KnownHostsCommand, Match exec, Include or provider
  option, no `-F` with a config outside ~/.ssh (ssh_runs_local), no shell separator or substitution
  inside the ssh command, no git option that runs one (GIT_RUNS_RE). Every other mention of a key
  path is refused (an exported GIT_SSH_COMMAND too). The same calls may name ssh's own settings files
  in ~/.ssh by any name (ssh_files): the direct value of `-F` or of `-o UserKnownHostsFile=` or
  `GlobalKnownHostsFile=`, when each file exists, is no private key and is shaped like that option's
  file (ssh_own_file: ssh_config keyword lines for -F, known_hosts entries for a host list); a host
  list may also be a missing file named like ssh's own (ssh_host_list). Any other mention of such a
  file is refused like a key's. Also passing: a key path handed to ssh-add or
  ssh-keygen, a config given by its tool's option (`kubectl --kubeconfig`, CONFIG_OPTIONS) or
  variable (`KUBECONFIG=`, CONFIG_VARS), mkdir, chmod, chown, touch and test of them, and ls or stat
  of a store folder itself (any other mention of the folder, or a glob naming it, does not; a bare
  folder name counts only where its path may lead to HOME, so a repo's own folder of that name passes);
  kubectl, docker, gcloud, aws and az read their own config unnamed. The words of a command ssh runs
  on another host are checked the same way, quotes are dropped before the check (~/.ss''h), and a
  find whose name test matches a private key or credential file is refused. Known gaps, accepted: a
  path built at run time inside a script or inline code; a recursive read of all of HOME or
  ~/.config (`grep -r x ~`); programs git starts on its own that inherit its ssh command (hooks or
  config an earlier call set up); ssh options in a config file an earlier call wrote, or in one in
  ~/.ssh given by -F (a ProxyCommand there runs, as it does from ~/.ssh/config); a key path
  read back from the process table of a running ssh.
Secrets stay reachable only through `ttp secret env` (hook.secret_read).

Pushes go only through `ttp push` and the push queue. The run's bin/gh (prguard.check) refuses, inside
a run, `gh api` calls that create, move or delete refs or tags, write file contents, merge branches,
update a PR's branch, write releases or the rules that protect a branch or tag (branch protection,
tag protection, rulesets; ref_write), the gh commands that send the same requests (`gh release`
create, edit, delete, upload and delete-asset, `gh pr update-branch`, `gh repo sync` of a named repo),
and the GraphQL mutations doing the same; the hook refuses them sent by curl and the like
(hook.api_ref_write). Reads pass. PR merges stay under the charter's merge policy and are not refused
here (`gh pr merge` is not either).
A session coordinator's Read, Grep and Glob calls are refused on the files credential_files() named at
launch and on what tool_refusal refuses (coordtools.pre_tool_use).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# ~/.ssh files that hold no secret
SSH_PUBLIC = {"config", "known_hosts", "known_hosts.old", "authorized_keys", "authorized_keys2", "rc", "environment"}
# A path under .ssh (the dir alone has no name), ending where a shell word ends
SSH_PATH_RE = re.compile(r"(?<![\w.-])\.ssh(?:/+([^\s\"'`;&|<>()]*))?(?![\w.-])")
# The other stores by name, or by the variable that moves them
STORE_RE = re.compile(r"(?:(?<![\w.-])gh|GH_CONFIG_DIR\}?)/+hosts\.ya?ml\b|(?<![\w.-])\.git-credentials\b"
                      r"|(?<![\w.-])\.netrc(?![\w.-])|(?<![\w.-])\.credentials\.json\b"
                      r"|(?:(?<![\w.-])\.codex|CODEX_HOME\}?)/+auth\.json\b"
                      r"|(?:(?<![\w.-])\.docker|DOCKER_CONFIG\}?)/+config\.json\b|(?<![\w.-])\.kube/+config\b"
                      r"|(?<![\w.-])\.pypirc(?![\w.-])|(?:(?<![\w.-])\.config|XDG_CONFIG_HOME\}?)/+git/+credentials(?![\w.-])")
# Store folders (the variable that moves each) and the entries in them that hold a token or key; their
# other entries hold none (aws `config`, gcloud `configurations`, azure `azureProfile.json`, kube `cache`,
# docker `buildx`, gh `config.yml`). A bare folder counts (`grep -r token ~/.kube`), but to ls, stat or mkdir it
DIR_STORES = {".aws": (None, ("credentials", "sso", "cli")),
              ".config/gcloud": ("CLOUDSDK_CONFIG", ("credentials.db", "access_tokens.db", "legacy_credentials",
                                                    "application_default_credentials.json")),
              ".azure": ("AZURE_CONFIG_DIR", ("accessTokens.json", "msal_token_cache.json", "msal_token_cache.bin",
                                              "service_principal_entries.json", "service_principal_entries.bin")),
              ".kube": (None, ("config",)), ".docker": ("DOCKER_CONFIG", ("config.json",)),
              ".config/git": (None, ("credentials",)), ".config/gh": ("GH_CONFIG_DIR", ("hosts.yml", "hosts.yaml"))}
DIR_RES = {d: re.compile(r"(?<![\w.-])" + re.escape(d).replace("/", "/+") + r"(?:/+([^\s\"'`;&|<>()]*))?(?![\w.-])")
           for d in DIR_STORES}
# A tool's option or a variable that hands the tool its own config file: the tool reads it, the run does not
CONFIG_OPTIONS = {"kubectl": "--kubeconfig", "helm": "--kubeconfig", "k9s": "--kubeconfig", "twine": "--config-file",
                  "docker": "--config"}
CONFIG_VARS = ("KUBECONFIG=", "DOCKER_CONFIG=", "AWS_SHARED_CREDENTIALS_FILE=", "CLOUDSDK_CONFIG=",
               "AZURE_CONFIG_DIR=", "GOOGLE_APPLICATION_CREDENTIALS=", "GH_CONFIG_DIR=")
SSH_FAMILY = {"ssh", "scp", "sftp", "autossh", "ssh-copy-id"}
# The ssh -o option that names a key ssh reads itself
SSH_KEY_OPTION_RE = re.compile(r"\s*IdentityFile[\s=]", re.I)
# the host lists ssh reads (and adds to) itself: one or more files after the option name
SSH_HOSTS_OPTION_RE = re.compile(r"(?i)\s*(?:user|global)knownhostsfile(?:\s*=\s*|\s+)(.*)")
SSH_KEY_NAME_RE = re.compile(r"^id_(?!.*\.pub$)|\.(?:pem|key|ppk|p12|pfx)$")
# The ssh_config(5) keywords: every line of a file passed as ssh -F starts with one (ssh_own_file)
SSH_CONFIG_WORDS = frozenset(w.lower() for w in """
    Host Match AddKeysToAgent AddressFamily BatchMode BindAddress BindInterface CanonicalDomains
    CanonicalizeFallbackLocal CanonicalizeHostname CanonicalizeMaxDots CanonicalizePermittedCNAMEs
    CASignatureAlgorithms CertificateFile ChannelTimeout CheckHostIP Ciphers ClearAllForwardings Compression
    ConnectionAttempts ConnectTimeout ControlMaster ControlPath ControlPersist DynamicForward EnableEscapeCommandline
    EnableSSHKeysign EscapeChar ExitOnForwardFailure FingerprintHash ForkAfterAuthentication ForwardAgent
    ForwardX11 ForwardX11Timeout ForwardX11Trusted GatewayPorts GlobalKnownHostsFile GSSAPIAuthentication
    GSSAPIDelegateCredentials HashKnownHosts HostbasedAcceptedAlgorithms HostbasedAuthentication
    HostKeyAlgorithms HostKeyAlias Hostname IdentitiesOnly IdentityAgent IdentityFile IgnoreUnknown Include
    IPQoS KbdInteractiveAuthentication KbdInteractiveDevices KexAlgorithms KnownHostsCommand LocalCommand
    LocalForward LogLevel LogVerbose MACs NoHostAuthenticationForLocalhost NumberOfPasswordPrompts
    ObscureKeystrokeTiming PasswordAuthentication PermitLocalCommand PermitRemoteOpen PKCS11Provider Port
    PreferredAuthentications ProxyCommand ProxyJump ProxyUseFdpass PubkeyAcceptedAlgorithms
    PubkeyAuthentication RekeyLimit RemoteCommand RemoteForward RequestTTY RequiredRSASize RevokedHostKeys
    SecurityKeyProvider SendEnv ServerAliveCountMax ServerAliveInterval SessionType SetEnv StdinNull
    StreamLocalBindMask StreamLocalBindUnlink StrictHostKeyChecking SyslogFacility TCPKeepAlive Tag Tunnel
    TunnelDevice UpdateHostKeys User UserKnownHostsFile VerifyHostKeyDNS VisualHostKey XAuthLocation""".split())
# One known_hosts(5) entry: an optional marker, host patterns or a |1| hash, a key type and its base64 key
SSH_HOSTS_LINE_RE = re.compile(r"(?:@(?:cert-authority|revoked)\s+)?\S+\s+(?:ssh|ecdsa|sk|rsa)-[\w@.-]+"
                               r"\s+[A-Za-z0-9+/]+={0,2}(?:\s.*)?")
# ssh -o options that run a local program or read another config or library
SSH_RUNS_RE = re.compile(r"(?i)\s*(?:proxycommand|localcommand|permitlocalcommand|knownhostscommand|match|include"
                         r"|pkcs11provider|securitykeyprovider|xauthlocation)\b")
# Per client: its options that take a value, and those that run a local program or read another config
SSH_OPTS = {"ssh": ("BbcDEeFIiJLlmOoPpQRSWw", "FI"), "autossh": ("BbcDEeFIiJLlMmOoPpQRSWw", "FI"),
            "scp": ("cDFiJloPSX", "DFS"), "sftp": ("BbcDFiJloPRSsX", "bDFS"), "ssh-copy-id": ("FiopPt", "F")}
# Shell syntax that makes an ssh command string more than one plain ssh call
SHELL_META_RE = re.compile(r"[;&|<>`\n]|\$\(")
# The git subcommands a key ssh command may serve, and git words that run a local program
KEY_GIT = {"fetch", "clone", "ls-remote"}
GIT_RUNS_RE = re.compile(r"-[cu].*|ext::.*|--(?:config|config-env|upload-pack|receive-pack|template|exec-path|exec)"
                         r"(?:=.*)?")
# Commands that never print a file's contents or name; ls and stat may name the ~/.ssh dir only
SILENT = {"mkdir", "chmod", "chown", "touch", "test", "[", "ssh-add", "ssh-keygen"}
LISTERS = {"ls", "stat"}
# stat's format options: a stat with one prints only what its format asks for (stat has no escape for contents)
STAT_FORMAT_RE = re.compile(r"-c.|--format(=|$)|--printf(=|$)")
# A pipe, a substitution or a file redirect (not 2>) that could hand a listed key name to a reader
HANDS_ON_RE = re.compile(r"(?<!\|)\|(?!\|)|`|\$\(|<\(|(?<!2)>")
# The variable that holds the ssh command a client runs, given right before that client
SSH_COMMAND_VARS = {"git": "GIT_SSH_COMMAND=", "rsync": "RSYNC_RSH="}
# ssh options that take a value (the next word, or the rest of the same word)
SSH_VALUE_FLAGS = set(SSH_OPTS["ssh"][0])
# Names find may look for that only private keys and credential files have
FIND_SECRET_NAMES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ecdsa_sk", "id_ed25519_sk",
                     ".git-credentials", ".netrc", ".pypirc")
FIND_NAME_TESTS = {"-name": False, "-iname": True}
WHY = ("a run never reads credential stores (ssh private keys, gh's hosts file or `gh auth token`, git and netrc "
       "credential files, the agents' login files, cloud, docker, kube and PyPI credentials): what it prints stays "
       "in the run's log and transcript. ssh, git, gh and the cloud tools read them themselves: give ssh a key with "
       "`-i <key>`, give a stored secret to a command with `ttp secret env`, and call a store by what it is in "
       "text, never by its path")

# GitHub REST routes that move refs or write a repo's files: refs, tags, contents, merges, releases, renames,
# a PR branch update, and the rules that protect a branch or tag (a branch name may hold slashes)
REF_ROUTE_RE = re.compile(r"^/?(?:repos/[^/]+/[^/]+|repositories/\d+)/(?:git/refs|git/tags|contents|merges"
                          r"|merge-upstream|releases|branches/.+?/(?:rename|protection)|tags/protection|rulesets"
                          r"|pulls/\d+/update-branch)(?:/|$)", re.I)
# The same routes in a URL a client sends to
REF_URL_RE = re.compile(r"/(?:repos/[^/\s]+/[^/\s]+|repositories/\d+)/(?:git/refs|git/tags|contents/|merges\b"
                        r"|merge-upstream\b|releases\b|branches/\S+?/(?:rename|protection)\b|tags/protection\b"
                        r"|rulesets\b|pulls/\d+/update-branch\b)", re.I)
REF_MUTATION_RE = re.compile(r"\b(?:createRef|updateRefs?|deleteRef|createCommitOnBranch|mergeBranch"
                             r"|updatePullRequestBranch|(?:create|update|delete)BranchProtectionRule"
                             r"|(?:create|update|delete)RepositoryRuleset)\b")
# gh release subcommands that write a release (create and publish also make its tag)
RELEASE_WRITES = {"create", "edit", "delete", "upload", "delete-asset"}
REF_WHY = ("refused: a run never writes git refs, tags, releases, file contents or branch rules through the GitHub "
           "API (gh api, gh release, gh pr update-branch, gh repo sync, curl and the like). Commit, and land the "
           "change through `ttp push` and the push queue, which run the checks and review first")


def ssh_public(name: str) -> bool:
    """`name` (a path under ~/.ssh) holds no secret: a public key (a glob of them too), ssh's config or
    host lists (known_hosts anywhere in the file name: known_hosts_<site>, <site>_known_hosts), or a
    control socket (cm-*, or a name with ssh's % tokens)."""
    if ".." in name.split("/"):
        return False
    base = name.rstrip("/").rsplit("/", 1)[-1]
    if base.endswith(".pub"):
        return True
    if not base or re.search(r"[*?\[]", name):
        return False
    stem = base.split(".", 1)[0].rstrip("~")   # ssh's client config and its temp and backup copies: settings
    return (base in SSH_PUBLIC or base.startswith(("cm-", "config-", "config~")) or "known_hosts" in base or "%" in base
            or (stem == "config" or stem.endswith("_config")) and not base.endswith((".key", ".pem"))
            or name.split("/", 1)[0] == "config.d")


def dir_public(store: str, name: str) -> bool:
    """`name` (a path under the DIR_STORES folder `store`) holds no token or key: it is none of the
    folder's secret entries, under one, a glob that takes one, or the folder itself (`./` parts dropped)."""
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return False
    first = parts[0]
    from fnmatch import fnmatchcase
    return not any(fnmatchcase(n, first) for n in DIR_STORES[store][1])


def _stores(home: Path) -> list[Path]:
    """The credential files and folders but ~/.ssh's, where their variables put them (missing ones too)."""
    env = os.environ.get
    config = Path(env("XDG_CONFIG_HOME") or home / ".config")
    out = [Path(env("GH_CONFIG_DIR") or config / "gh") / "hosts.yml", home / ".git-credentials", home / ".netrc",
           Path(env("CLAUDE_CONFIG_DIR") or home / ".claude") / ".credentials.json",
           Path(env("CODEX_HOME") or home / ".codex") / "auth.json",
           Path(env("DOCKER_CONFIG") or home / ".docker") / "config.json", home / ".pypirc",
           config / "git" / "credentials",
           *[Path(p) for p in (env("KUBECONFIG") or str(home / ".kube" / "config")).split(os.pathsep) if p]]
    if env("AWS_SHARED_CREDENTIALS_FILE"):
        out.append(Path(env("AWS_SHARED_CREDENTIALS_FILE")))
    for d, (var, names) in DIR_STORES.items():
        out += [Path(var and env(var) or home / d) / n for n in names]
    return list(dict.fromkeys(out))


def credential_files(home: str | Path | None = None) -> list[str]:
    """The credential files a run's Read tool may not open (absolute paths, missing ones too, but the
    ~/.ssh files that exist and are not public)."""
    home = Path(home or os.path.expanduser("~"))
    out = _stores(home)
    try:
        out += sorted(p for p in (home / ".ssh").iterdir() if p.is_file() and not ssh_public(p.name))
    except OSError:
        pass
    return [str(p) for p in out]


def _passing_words(argv: list[str], hands_on: bool = True) -> list[str]:
    """The words of `argv` that may name a key: an ssh key option's value, every word of a SILENT
    command, a LISTERS word naming only the ~/.ssh dir, and every word of a stat with a format option
    when the command hands no output on (`hands_on` False)."""
    name = os.path.basename(argv[0])
    if name in SILENT or (name == "stat" and not hands_on
                          and any(STAT_FORMAT_RE.match(w) or w == "-c" for w in argv[1:])):
        return argv[1:]
    if name in LISTERS:
        return [w for w in argv[1:] if not any(m.group(1) for rx in (SSH_PATH_RE, *DIR_RES.values())
                                               for m in rx.finditer(w))]
    option = CONFIG_OPTIONS.get(name)
    if option:
        return ([w for prev, w in zip(argv[1:], argv[2:]) if prev == option]
                + [w for w in argv[1:] if w.startswith(option + "=")])
    return ssh_keys(argv) + ssh_files(argv) if name in SSH_FAMILY else []


def ssh_options(argv: list[str]) -> list[tuple[str, str, str]]:
    """(option letter, value, the word holding the value) for each option of an ssh, scp or sftp call.
    Options end at the first operand, for ssh at the one after its host: the rest is the remote command."""
    name = os.path.basename(argv[0])
    takes = SSH_OPTS.get(name, SSH_OPTS["ssh"])[0]
    out, i, host = [], 1, name not in ("ssh", "autossh")
    while i < len(argv) and argv[i] != "--":
        w = argv[i]
        if not w.startswith("-") or w == "-":
            if host:
                break
            host, i = True, i + 1
            continue
        j = next((k for k, c in enumerate(w[1:], 1) if c in takes), None)
        out += [(c, "", "") for c in w[1:j]]
        if j is None:
            i += 1
            continue
        tail = w[j + 1:]
        val = tail or (argv[i + 1] if i + 1 < len(argv) else "")
        out.append((w[j], val, w if tail else val))
        i += 1 if tail else 2
    return out


def ssh_own_file(value: str, kind: str = "config") -> bool:
    """`value` names one of ssh's own settings files in ~/.ssh, as ~/, $HOME/ or an absolute path, shaped like
    the option it is given to (fail closed): for `kind` "config" (-F) every line that is not blank or a comment
    starts with an ssh_config keyword, for "hosts" (UserKnownHostsFile, GlobalKnownHostsFile) every such line is
    a known_hosts entry. Any other file, a token or age key too, is refused: ssh prints the first word of each
    config line it cannot parse, and it adds host lines to a host list. The file is no private key either,
    neither by its name (id_*, *.pem, *.key, ...) nor by its content."""
    home = os.path.expanduser("~")
    path = re.sub(r"^\$(?:HOME|\{HOME\})(?=/)", lambda _: home, value)
    if not path.startswith(("~/", "/")) or re.search(r"[*?\[%$`]", path):
        return False
    ssh, real = os.path.realpath(os.path.join(home, ".ssh")), os.path.realpath(os.path.expanduser(path))
    if not real.startswith(ssh + os.sep) or SSH_KEY_NAME_RE.search(os.path.basename(real)) or os.path.isdir(real):
        return False
    try:
        with open(real, "rb") as f:
            data = f.read(1 << 20)
            if f.read(1):   # over 1 MiB: no settings file ssh keeps
                return False
    except OSError:   # missing or unreadable: none (ssh would make a new host list there)
        return False
    if b"PRIVATE KEY" in data or b"PuTTY-User-Key" in data:
        return False
    lines = [ln.strip() for ln in data.decode("latin-1").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if kind == "hosts":
        return all(SSH_HOSTS_LINE_RE.fullmatch(ln) for ln in lines)
    return all(re.split(r"[\s=]", ln, maxsplit=1)[0].lower() in SSH_CONFIG_WORDS for ln in lines)


def ssh_host_list(value: str) -> bool:
    """`value` is a host list ssh may read and add to: a known_hosts-shaped file in ~/.ssh (ssh_own_file), or one
    named as ssh names its host lists (known_hosts in the file name, ssh_public) while it is missing, which ssh
    makes. An existing file passes only by its shape, whatever its name. ssh's config, authorized_keys and any
    other file are no host list: ssh would add host lines to them."""
    if ssh_own_file(value, "hosts"):
        return True
    path = re.sub(r"^(?:~|\$(?:HOME|\{HOME\}))/", "", value)
    base = os.path.basename(path)
    return (path != value and path.startswith(".ssh/") and "known_hosts" in base and ssh_public(path[5:])
            and not re.search(r"[*?\[%$`]", path)
            and not os.path.lexists(os.path.join(os.path.expanduser("~"), path)))


def ssh_files(argv: list[str]) -> list[str]:
    """The words naming ssh's own settings files in an ssh, scp or sftp call: the value of -F, or of
    -o UserKnownHostsFile= or GlobalKnownHostsFile=, when each file it names passes ssh_own_file for that option."""
    out = []
    for o, v, word in ssh_options(argv):
        m = SSH_HOSTS_OPTION_RE.match(v) if o == "o" else None
        files = [v] if o == "F" else m[1].split() if m else []
        if files and all(ssh_own_file(f, "config") if o == "F" else ssh_host_list(f) for f in files):
            out.append(word)
    return out


def ssh_runs_local(argv: list[str]) -> bool:
    """An ssh, scp or sftp call whose options run a local program or read another config (-F with a
    config outside ~/.ssh (ssh_own_file), ProxyCommand, LocalCommand, KnownHostsCommand, Match exec,
    Include, a provider library, scp -S)."""
    runs = SSH_OPTS.get(os.path.basename(argv[0]), SSH_OPTS["ssh"])[1]
    return any(o in runs and not (o == "F" and ssh_own_file(v)) or o == "o" and SSH_RUNS_RE.match(v)
               for o, v, _ in ssh_options(argv))


def ssh_keys(argv: list[str]) -> list[str]:
    """The words naming the key of a plain ssh, scp or sftp call (its -i or -o IdentityFile values),
    none when its options run a local program."""
    if ssh_runs_local(argv):
        return []
    return [word for o, v, word in ssh_options(argv) if o == "i" or o == "o" and SSH_KEY_OPTION_RE.match(v + " ")]


def _prints_token(argv: list[str]) -> bool:
    name, args = os.path.basename(argv[0]), argv[1:]
    if name == "gh":
        return args[:2] == ["auth", "token"] or (args[:2] == ["auth", "status"]
                                                 and any(a in ("-t", "--show-token") for a in args))
    if name == "git" and "credential" in args:
        return args[args.index("credential") + 1:][:1] == ["fill"]
    return False


def ssh_remote(argv: list[str]) -> str:
    """The command an ssh call runs on the other host (its words after the destination), else ''."""
    if not argv or os.path.basename(argv[0]) not in ("ssh", "autossh"):
        return ""
    i = 1
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "-":
        if argv[i] == "--":
            i += 1
            break
        flags = argv[i][1:]
        j = next((k for k, c in enumerate(flags) if c in SSH_VALUE_FLAGS), None)
        i += 2 if j is not None and j == len(flags) - 1 else 1
    return " ".join(argv[i + 1:])


def ssh_commands(raw: list[str]) -> list[str]:
    """The ssh commands a plain git or rsync call `raw` (assignments before it kept) hands its client
    as one string: GIT_SSH_COMMAND or RSYNC_RSH right before it, rsync's -e/--rsh, git's -c
    core.sshCommand. None when git runs a local program (GIT_RUNS_RE) or serves anything but KEY_GIT."""
    from .hook import _strip_prefix
    argv = _strip_prefix(raw)
    name = os.path.basename(argv[0]) if argv else ""
    if name not in SSH_COMMAND_VARS:
        return []
    var = SSH_COMMAND_VARS[name]
    out = [w[len(var):] for w in raw[:len(raw) - len(argv)] if w.startswith(var)]
    if name == "git":
        i = 1
        while argv[i:i + 1] in (["-C"], ["-c"]) and i + 1 < len(argv):
            if argv[i] == "-c":
                if not argv[i + 1].lower().startswith("core.sshcommand="):
                    return []
                out.append(argv[i + 1].split("=", 1)[1])
            i += 2
        if argv[i:i + 1] == [] or argv[i] not in KEY_GIT or any(GIT_RUNS_RE.fullmatch(a) for a in argv[i + 1:]):
            return []
        return out
    for prev, w in zip(argv, argv[1:]):
        if prev == "--rsh" or re.fullmatch(r"-[a-zA-Z]*e", prev):
            out.append(w)
        elif w.startswith("--rsh="):
            out.append(w[6:])
        elif re.match(r"-[a-zA-Z]*e.", w) and not w.startswith("--"):
            out.append(w[w.index("e") + 1:])
    return out


def _finds_secret(argv: list[str]) -> bool:
    """A find whose -name or -iname pattern matches a private key or credential file name, with a
    literal part beyond its wildcards (so `-name '*'` and `-name '*.py'` pass)."""
    if not argv or os.path.basename(argv[0]) != "find":
        return False
    from fnmatch import fnmatchcase
    for flag, pat in zip(argv[1:], argv[2:]):
        if flag not in FIND_NAME_TESTS or not re.search(r"\w", re.sub(r"\[[^\]]*\]|[*?]", "", pat)):
            continue
        fold = (lambda t: t.lower()) if FIND_NAME_TESTS[flag] else (lambda t: t)
        if any(fnmatchcase(fold(n), fold(pat)) for n in FIND_SECRET_NAMES):
            return True
    return False


def _unquoted(text: str) -> str:
    return re.sub(r"[\"'\\]", "", text)


def bash_refusal(cmd: str, segments: list[list[str]], home: str | Path | None = None,
                 cwd: str | None = None) -> str | None:
    """Why a Bash call reads a credential store, else None: it names one by path, other than as a
    word a passing command takes (each such word excuses one mention), prints a token, or finds a
    key by name. The command an ssh call runs remotely counts as its own commands."""
    from .hook import _all_segments, _segments
    segments = [argv for argv in segments if argv]
    # ssh commands handed to rsync or git (env assignments read before the parser drops them, each
    # value once, as both parses see it); only a value that is one plain ssh call counts, so other
    # words excuse nothing
    values = dict.fromkeys(v for argv in segments + _segments(cmd, strip=False) for v in ssh_commands(argv))
    segments += [inner[0] for value in values if not SHELL_META_RE.search(value)
                 for inner in [_all_segments(value)] if len(inner) == 1 and inner[0]
                 and os.path.basename(inner[0][0]) in SSH_FAMILY]
    segments += [inner for argv in segments for inner in _all_segments(ssh_remote(argv))]
    if any(_prints_token(argv) or _finds_secret(argv) for argv in segments):
        return WHY
    rest = _unquoted(cmd)
    hands_on = bool(HANDS_ON_RE.search(rest))
    stores = [str(p) for p in _stores(Path(home or os.path.expanduser("~")))]
    # with the wrappers the parser drops (`sudo -i`, `env -C ~`)
    cwds = _cwds(segments + _segments(cmd, strip=False), cwd or os.getcwd(), str(home or os.path.expanduser("~")))
    words = [w for argv in segments for w in _passing_words(argv, hands_on)]
    # a config variable's value, also as an assignment the parser drops (`KUBECONFIG=<file> kubectl ...`)
    words += [w for argv in _segments(cmd, strip=False) for w in argv if w.startswith(CONFIG_VARS)]
    for w in map(_unquoted, words):
        if ".ssh" in w or _names_store(w, stores, cwds):   # the whole word, never part of a longer path
            rest = re.sub(r"(?<![^\s;&|()<>])" + re.escape(w) + r"(?![^\s;&|()<>])", " ", rest, count=1)
    if _names_store(rest, stores, cwds) or any(not ssh_public(m.group(1) or "") for m in SSH_PATH_RE.finditer(rest)):
        return WHY
    # a glob the shell expands to a store or into one (`~/.kube/conf?g`, `$HOME/.py*`), but to list it
    globs = [os.path.expandvars(os.path.expanduser(w.split("=", 1)[-1])) for argv in segments
             if os.path.basename(argv[0]) not in SILENT | LISTERS for w in argv[1:] if re.search(r"[*?\[]", w)]
    if globs and any(_glob_takes(g, s) for g in globs if g.startswith("/") for s in credential_files(home)):
        return WHY
    # or to a store's folder itself (`grep -r token ~/.kub?`)
    home = Path(home or os.path.expanduser("~"))
    folders = [str(Path(var and os.environ.get(var) or home / d)) for d, (var, _) in DIR_STORES.items()]
    if any(g.rstrip("/").count("/") == f.count("/") and _glob_takes(g, f)
           for g in globs if g.startswith("/") for f in folders + [str(home / ".ssh")]):
        return WHY
    return None


def _glob_takes(pattern: str, path: str) -> bool:
    """The shell expands `pattern` to `path` or a path under it: one glob per path part, a dot name only
    by a dot pattern."""
    from fnmatch import fnmatchcase
    pp, sp = pattern.rstrip("/").split("/"), path.split("/")
    return len(pp) >= len(sp) and all(fnmatchcase(s, p) and (s[:1] != "." or p[:1] == ".") for p, s in zip(pp, sp))


def _names_store(text: str, stores: list[str], cwds: list[str] | None = None) -> bool:
    """`text` names a store but ~/.ssh: by its usual name (STORE_RE, a DIR_STORES secret entry or the
    folder itself) or by the path its variable gives it. With `cwds` (_cwds), the folder itself counts
    only where the path before it in its word may lead to HOME (_homeward)."""
    def counts(m: re.Match) -> bool:
        if cwds is None or [p for p in (m.group(1) or "").split("/") if p not in ("", ".")]:
            return True
        start = max(text.rfind(c, 0, m.start()) for c in " \t\n;&|(<>") + 1   # `$(echo ~)/` stays one word
        return _homeward(text[start:m.start()].rsplit("=", 1)[-1], cwds)
    return bool(STORE_RE.search(text)) or any(p in text for p in stores) or any(
        not dir_public(d, m.group(1) or "") and counts(m) for d, rx in DIR_RES.items() for m in rx.finditer(text))


def _resolve(path: str, cwds: list[str], home: str) -> list[str] | None:
    """Where `path` leads from each of `cwds` (~, $HOME and ${HOME} put at `home`), None for a path the
    shell expands some other way (another variable, a substitution, a brace, ~user, `host:`)."""
    for h in ("~", "${HOME}", "$HOME"):
        if path == h or path.startswith(h + "/"):
            path = home + path[len(h):]
            break
    if not re.fullmatch(r"[\w./+@%-]*", path):
        return None
    try:
        return [os.path.realpath(os.path.join(c, path)) for c in cwds]
    except (OSError, ValueError):
        return None


def _homeward(prefix: str, cwds: list[str]) -> bool:
    """A store folder after `prefix` may be HOME's: `prefix` leads to HOME (cwds[0]) from a folder the
    command may run in (cwds[1:]), or the shell expands it some other way."""
    places = _resolve(prefix, cwds[1:], cwds[0])
    return places is None or cwds[0] in places


def _cwds(segments: list[list[str]], cwd: str, home: str) -> list[str]:
    """HOME, then the folders a relative path in the command may start from: `cwd`, each cd or pushd
    target, and HOME again when a word may name it (`tar -C ~`, `cd ..`, `$D`) or a remote shell or
    another user's shell runs (ssh, su, sudo)."""
    home = os.path.realpath(home)
    cwds = [home, os.path.realpath(cwd)]
    for argv in segments:
        name = os.path.basename(argv[0])
        targets = [w for w in argv[1:] if not w.startswith("-")] if name in ("cd", "pushd") else []
        if name in SSH_FAMILY | {"su", "sudo", "doas", "runuser", "popd"} or name in ("cd", "pushd") and not targets:
            cwds.append(home)
        for w in argv[1:]:
            for v in {w.split("=", 1)[-1], w[2:] if re.match(r"-\w.", w) else ""} - {""}:
                places = _resolve(v, cwds[1:], home)
                if places is None and v[0] in "$~`" or places and home in places:
                    cwds.append(home)
                elif places and w in targets:
                    cwds += places
    return [home] + list(dict.fromkeys(cwds[1:]))


def ssh_secret(path: str, search: bool = False, home: str | Path | None = None) -> bool:
    """`path` is a non-public file in ~/.ssh (ssh_public), also one made after credential_files() listed
    them or reached by a symlink; with `search` (Grep, Glob), also the ~/.ssh dir itself."""
    try:
        ssh = os.path.realpath(Path(home or os.path.expanduser("~")) / ".ssh")
        real = os.path.realpath(os.path.expanduser(path))
    except (OSError, ValueError):
        return True
    if real == ssh:
        return search
    return real.startswith(ssh + os.sep) and not ssh_public(os.path.relpath(real, ssh))


READ_TOOLS = ("Read", "Grep", "Glob")


def read_refusal(path: str, search: bool = False, home: str | Path | None = None) -> bool:
    """Reading `path` reads a credential store (ssh_secret, or a credential_files() entry or a file in
    one), also by a symlink; with `search` (Grep, Glob), also walking a folder that holds one."""
    if ssh_secret(path, search, home):
        return True
    try:
        real = os.path.realpath(os.path.expanduser(path))
        files = [os.path.realpath(f) for f in credential_files(home)]
    except (OSError, ValueError):
        return True
    inside = real.rstrip(os.sep) + os.sep
    return any(real == f or real.startswith(f + os.sep) or search and f.startswith(inside) and os.path.lexists(f)
               for f in files)


def tool_refusal(tool: str, args: dict, cwd: str | None = None, home: str | Path | None = None) -> str | None:
    """Why a Read, Grep or Glob call reaches a credential store (read_refusal), else None. A Grep or
    Glob without a path searches `cwd`; a Glob pattern's fixed folder part (`~/.aws/*`) moves its root."""
    if tool not in READ_TOOLS:
        return None
    cwd = cwd or os.getcwd()
    search = tool != "Read"
    path = os.path.expanduser(str(args.get("file_path") or args.get("path") or (cwd if search else "")))
    if tool == "Glob":
        fixed = re.split(r"[*?\[{]", str(args.get("pattern") or ""), maxsplit=1)[0]
        path = os.path.join(path, os.path.expanduser(fixed.rsplit("/", 1)[0] if "/" in fixed else ""))
    if path and read_refusal(os.path.join(cwd, path), search, home):
        return "that file holds a credential, or the search walks into one (search a narrower folder): " + WHY
    return None


def ref_write(endpoint: str, method: str, text: str) -> bool:
    """A `gh api` call to `endpoint` with `method` (its fields and body in `text`) writes refs, tags,
    releases or file contents."""
    if endpoint.strip("/").lower() == "graphql":
        return bool(REF_MUTATION_RE.search(text))
    return method not in ("GET", "HEAD") and bool(REF_ROUTE_RE.match(endpoint))


def gh_refusal(args: list[str]) -> str | None:
    """Why gh `args` (no `api`) writes a ref or a release, inside a run: a release write
    (RELEASE_WRITES), pr update-branch, repo sync of a named repo."""
    if args[:1] == ["release"]:
        sub = next((a for i, a in enumerate(args[1:], 1)
                    if not a.startswith("-") and args[i - 1] not in ("-R", "--repo")), "")
        return REF_WHY if sub in RELEASE_WRITES else None
    if args[:2] == ["pr", "update-branch"]:
        return REF_WHY
    if args[:2] == ["repo", "sync"]:
        values = {"-s", "--source", "-b", "--branch"}
        if any(not a.startswith("-") and args[i - 1] not in values for i, a in enumerate(args[2:], 2)):
            return REF_WHY
    return None
