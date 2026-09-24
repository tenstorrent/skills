#!/usr/bin/env bash

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

# SPDX-License-Identifier: Apache-2.0

# Shared detection: should tt-buddy load for this session?
# Sourced by hooks/session-start and hooks/user-prompt-submit.
#
# Loads if any of:
#   1. /dev/tenstorrent exists (we're on TT hardware)
#   2. CWD is inside a Git checkout with a Tenstorrent GitHub remote
#   3. Such a checkout is a direct child
#   4. Such a checkout is a second-level descendant
#   5. Such a checkout is a sibling
#
# Returns 0 (TT context) or 1 (not TT context).

has_tenstorrent_remote() {
    local repo_root="$1"
    local remote
    local url

    while IFS= read -r remote; do
        url="$(git -C "$repo_root" remote get-url "$remote" 2>/dev/null || true)"
        case "$url" in
            https://github.com/tenstorrent/* | \
            git@github.com:tenstorrent/* | \
            ssh://git@github.com/tenstorrent/* | \
            git://github.com/tenstorrent/*) return 0 ;;
        esac
    done < <(git -C "$repo_root" remote 2>/dev/null)

    return 1
}

is_tenstorrent_checkout() {
    local candidate="${1%/}"
    [ -e "$candidate/.git" ] && has_tenstorrent_remote "$candidate"
}

is_tt_context() {
    local cwd="$1"
    [ -z "$cwd" ] && cwd="$PWD"

    # 1. TT hardware
    [ -e /dev/tenstorrent ] && return 0

    # 2. CWD is inside a Tenstorrent Git checkout
    local repo_root
    repo_root="$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null || true)"
    [ -n "$repo_root" ] && has_tenstorrent_remote "$repo_root" && return 0

    # 3-5. Check nearby Git checkouts
    local candidate
    for candidate in "$cwd"/*/ "$cwd"/*/*/ "$cwd"/../*/; do
        is_tenstorrent_checkout "$candidate" && return 0
    done

    return 1
}

# Read cwd from hook input JSON on stdin; fall back to $PWD.
read_hook_cwd() {
    local stdin_json
    stdin_json="$(cat 2>/dev/null || true)"
    local cwd=""
    if [ -n "$stdin_json" ] && command -v jq >/dev/null 2>&1; then
        cwd="$(echo "$stdin_json" | jq -r '.cwd // empty' 2>/dev/null || echo "")"
    fi
    [ -z "$cwd" ] && cwd="$PWD"
    printf '%s' "$cwd"
}
