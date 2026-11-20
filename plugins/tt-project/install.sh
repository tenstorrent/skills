#!/bin/sh
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Installs tt-project for each of Claude Code, Codex and Cursor found on this machine, then runs
# `ttp setup` and checks that the installed `ttp` runs. Safe to rerun: it updates the same clone and
# config entries in place. A step that fails does not stop the other tools or `ttp setup`: the
# failures are listed at the end, and the installer then exits 1.
#
# Usage:
#
# curl -fsSL https://raw.githubusercontent.com/tenstorrent/skills/HEAD/plugins/tt-project/install.sh | sh
#
# HEAD is the default branch. To install a release tag (or branch), name it in the URL and here:
#
#   curl -fsSL https://raw.githubusercontent.com/tenstorrent/skills/<tag>/plugins/tt-project/install.sh | TT_PROJECT_REF=<tag> sh
#
# Piped like that (or downloaded and run with sh), it fetches TT_PROJECT_REF (a release tag, branch or commit; default: the default
# branch) of TT_PROJECT_REPO (default: the public repository) into one clone kept at
# $XDG_DATA_HOME/tt-project/skills (default ~/.local/share/tt-project/skills), then runs that
# commit's own install.sh. `sh <checkout>/plugins/tt-project/install.sh` installs the commit that
# checkout has instead. Claude Code and Codex use the clone as a local marketplace, and Cursor gets
# a copy of its plugin folder.
#
# It fetches the one ref and checks out FETCH_HEAD, rather than `git clone --branch`, which warns
# that an annotated tag "is not a commit". Piped, nothing runs before the line that calls
# fetch_and_run, so a download cut short does nothing.
#
# When ~/.local/bin (where `ttp setup` puts `ttp`) is not on PATH, it appends one line, marked
# "# added by tt-project installer", to the profile of your login shell ($SHELL), once: bash
# ~/.bashrc (on macOS, whose terminals start login shells, the first of ~/.bash_profile,
# ~/.bash_login and ~/.profile that exists, else a new ~/.bash_profile), zsh
# ${ZDOTDIR:-~}/.zshrc, fish ${XDG_CONFIG_HOME:-~/.config}/fish/conf.d/tt-project.fish, any other ~/.profile. It never
# changes other lines; `ttp uninstall` removes that line. TT_PROJECT_NO_MODIFY_PATH=1 leaves your
# profile alone and only says what to add.
set -eu

MARKET=tenstorrent-skills
PLUGIN=tt-project
REPO=https://github.com/tenstorrent/skills.git

say() { printf 'tt-project: %s\n' "$*"; }
die() { printf 'tt-project: %s\n' "$*" >&2; exit 1; }

need() {
    command -v git >/dev/null 2>&1 || die "git is required; install it, then rerun"
    command -v python3 >/dev/null 2>&1 || die "Python 3.9+ (python3) is required; install it, then rerun"
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null ||
        die "Python 3.9+ is required; python3 here is $(python3 -V 2>&1)"
}

data=${XDG_DATA_HOME:-$HOME/.local/share}/tt-project
dest=$data/skills

# Make $dest a git clone, or check that it is one. $made names what this run created.
use_dest() {
    made=""
    [ -e "$data" ] || made=$data
    if [ ! -e "$dest" ]; then
        made=${made:-$dest}
        mkdir -p "$data" && git init -q "$dest"
    elif ! git -C "$dest" rev-parse --git-dir >/dev/null 2>&1 || [ "$(cd "$dest" && git rev-parse --show-toplevel)" != "$(cd "$dest" && pwd -P)" ]; then
        die "$dest exists and is not a git clone; move it away and rerun"
    fi
}

# Piped from curl: fetch the ref into the kept clone, then run that commit's own installer.
fetch_and_run() {
    url=${TT_PROJECT_REPO:-$REPO} name=${TT_PROJECT_REF:-the default branch}
    use_dest
    if ! git -C "$dest" fetch -q --depth 1 "$url" "${TT_PROJECT_REF:-HEAD}" ||
        ! git -C "$dest" cat-file -e "FETCH_HEAD:plugins/$PLUGIN/install.sh" 2>/dev/null; then
        [ -z "$made" ] || rm -rf "$made"
        die "no installer at $name of $url; set TT_PROJECT_REF to a release tag"
    fi
    git -C "$dest" checkout -q -f --detach FETCH_HEAD
    git -C "$dest" remote set-url origin "$url" 2>/dev/null || git -C "$dest" remote add origin "$url"
    exec sh "$dest/plugins/$PLUGIN/install.sh"
}

# Run from a checkout (this file sits in its plugins/tt-project): install that checkout. Otherwise
# (piped, or downloaded and run on its own): fetch the ref. One `if`, so a cut-short line runs nothing.
if [ -f "$0" ] && [ -f "$(dirname "$0")/bin/ttp" ]; then need; else need; fetch_and_run; fi

src=$(cd "$(dirname "$0")/../.." && pwd -P)
[ -f "$src/plugins/$PLUGIN/bin/ttp" ] || die "$src has no plugins/$PLUGIN; run install.sh from a checkout of the skills repository"
git -C "$src" rev-parse --verify -q HEAD >/dev/null || die "$src is not a git checkout"

dest_real=$( [ -d "$dest" ] && cd "$dest" && pwd -P || echo "" )
if [ "$dest_real" != "$src" ]; then
    use_dest
    git -C "$dest" fetch -q --depth 1 "$src" HEAD
    git -C "$dest" checkout -q -f --detach FETCH_HEAD
    # a checkout without an origin: fall back to the public repository
    url=$(git -C "$src" remote get-url origin 2>/dev/null || true)
    url=${url:-$REPO}
    git -C "$dest" remote set-url origin "$url" 2>/dev/null || git -C "$dest" remote add origin "$url"
fi
plugin=$dest/plugins/$PLUGIN
[ -f "$plugin/bin/ttp" ] || die "$dest has no plugins/$PLUGIN after the update"
# The one-line install fetches the ref into FETCH_HEAD without a local tag: name the tag it fetched.
fetched_tag() {
    [ "$(git -C "$src" rev-parse -q --verify 'FETCH_HEAD^{commit}' 2>/dev/null)" = "$(git -C "$src" rev-parse HEAD)" ] || return 1
    t=$(sed -n "1s/^[0-9a-f]*[[:space:]]*tag '\(.*\)' of .*/\1/p" "$(git -C "$src" rev-parse --absolute-git-dir)/FETCH_HEAD")
    [ -n "$t" ] && printf '%s\n' "$t"
}
rev=$(git -C "$src" describe --tags --exact-match HEAD 2>/dev/null || fetched_tag || git -C "$src" rev-parse --short HEAD)
ver=$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$plugin/.claude-plugin/plugin.json" | head -1)
say "installing $PLUGIN ${ver:-?} ($rev) from $dest"

# One install step of $tool. A failure is listed at the end and skips only the rest of that tool's steps.
failed=""
fail() {
    printf 'tt-project: %s: %s\n' "$tool" "$*" >&2
    failed="$failed
tt-project:   $tool: $*"
}
step() {
    "$@" && return 0
    rc=$?
    fail "\`$*\` failed (exit $rc)"
    return 1
}

claude_steps() {
    step claude plugin marketplace add "$dest" &&
        step claude plugin install "$PLUGIN@$MARKET" &&
        step claude plugin update "$PLUGIN@$MARKET"    # an earlier install from another source moves to this one
}

codex_steps() {
    # Codex refuses a CODEX_HOME that does not exist yet (installed, never run).
    step mkdir -p "${CODEX_HOME:-$HOME/.codex}" || return 1
    if ! out=$(codex plugin marketplace add "$dest" 2>&1); then
        case $out in
        *"different source"*)
            say "Codex had $MARKET from another source: replacing it with $dest"
            step codex plugin marketplace remove "$MARKET" && step codex plugin marketplace add "$dest" || return 1 ;;
        *) printf '%s\n' "$out" >&2; fail "\`codex plugin marketplace add $dest\` failed"; return 1 ;;
        esac
    else
        printf '%s\n' "$out"
    fi
    step codex plugin add "$PLUGIN@$MARKET"    # copies the current files into Codex's plugin cache
}

# Cursor loads a symlink in its local folder only when it points inside that folder, so it gets a copy.
cursor_steps() {
    local_dir=$HOME/.cursor/plugins/local
    step mkdir -p "$local_dir" &&
        step rm -rf "$local_dir/.$PLUGIN.new" &&
        step cp -R "$plugin" "$local_dir/.$PLUGIN.new" &&
        step rm -rf "$local_dir/$PLUGIN" &&
        step mv "$local_dir/.$PLUGIN.new" "$local_dir/$PLUGIN" || return 1
    say "Cursor: copied the plugin to $local_dir/$PLUGIN"
}

# Put ~/.local/bin on PATH for new shells: one marked line in the login shell's profile, added once.
PATH_MARK="# added by tt-project installer"
add_to_path() {
    case ${SHELL:-} in
    */fish|fish)
        rc=${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d/tt-project.fish
        line="fish_add_path -g \$HOME/.local/bin $PATH_MARK" now='fish_add_path -g $HOME/.local/bin' ;;
    *)
        case ${SHELL:-} in
        */bash|bash)
            rc=$HOME/.bashrc
            if [ "$(uname -s 2>/dev/null)" = Darwin ]; then    # a login shell reads only the first that exists
                rc=$HOME/.bash_profile
                for f in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
                    [ ! -e "$f" ] || { rc=$f; break; }
                done
            fi ;;
        */zsh|zsh) rc=${ZDOTDIR:-$HOME}/.zshrc ;;
        *) rc=$HOME/.profile ;;
        esac
        line="export PATH=\"\$HOME/.local/bin:\$PATH\" $PATH_MARK" now='export PATH="$HOME/.local/bin:$PATH"' ;;
    esac
    old=$( [ -f "$rc" ] && cat "$rc"; printf x )    # the x keeps a last newline the $( ) would strip
    case $old in
    *"$PATH_MARK"*) say "  ttp: $rc already puts ~/.local/bin on PATH for new shells" ;;
    *)
        nl=""
        case $old in x|*"
x") ;; *) nl="
" ;; esac    # the file's last line has no newline: end it first
        if mkdir -p "$(dirname "$rc")" 2>/dev/null && printf '%s%s\n' "$nl" "$line" >> "$rc" 2>/dev/null; then
            say "  ttp: added ~/.local/bin to PATH for new shells in $rc"
        else
            say "  ttp: could not write $rc: add ~/.local/bin to PATH in your shell profile, then open a new shell"
            return 0
        fi ;;
    esac
    say "  ttp: to use it in this shell, run: $now"
}

found=""
done_hosts=""
if command -v claude >/dev/null 2>&1; then
    found=1 tool="Claude Code"
    claude_steps && done_hosts="$done_hosts claude"
else
    say "Claude Code not found (no claude on PATH): skipped"
fi

if command -v codex >/dev/null 2>&1; then
    found=1 tool=Codex
    codex_steps && done_hosts="$done_hosts codex"
else
    say "Codex not found (no codex on PATH): skipped"
fi

if command -v cursor-agent >/dev/null 2>&1 || command -v cursor >/dev/null 2>&1 || [ -d "$HOME/.cursor" ]; then
    found=1 tool=Cursor
    cursor_steps && done_hosts="$done_hosts cursor"
else
    say "Cursor not found (no cursor or cursor-agent on PATH, no ~/.cursor): skipped"
fi

tool="ttp setup"
ttp_ok=""
# Done only when the installed `ttp` itself runs, not just the plugin's copy.
step "$plugin/bin/ttp" setup && step "$HOME/.local/bin/ttp" --version && ttp_ok=1

[ -n "$found" ] || say "no Claude Code, Codex or Cursor found: install one, then rerun this installer"
if [ -n "$failed" ]; then say "finished with failed steps (listed below). To load it:"; else say "done. To load it:"; fi
for h in $done_hosts; do
    case $h in
    claude) say "  Claude Code: start a new session, or in an open one run /reload-plugins (and /reload-skills for the plain /tt-project)" ;;
    codex) say "  Codex: start a new session (open sessions do not see it)" ;;
    cursor) say "  Cursor: restart it, or run Developer: Reload Window" ;;
    esac
done
if [ -n "$ttp_ok" ]; then
    case ":$PATH:" in
    *":$HOME/.local/bin:"*) say "  ttp: ready in this shell" ;;
    *) if [ "${TT_PROJECT_NO_MODIFY_PATH:-}" = 1 ]; then
           say "  ttp: add ~/.local/bin to PATH in your shell profile, then open a new shell"
       else
           add_to_path
       fi ;;
    esac
fi
if [ -n "$failed" ]; then
    printf 'tt-project: these steps failed; fix them, then rerun this installer:%s\n' "$failed" >&2
    exit 1
fi
