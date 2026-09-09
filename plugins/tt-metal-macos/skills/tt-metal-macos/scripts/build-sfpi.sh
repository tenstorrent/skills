#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

if [[ $# != 2 || ${1:-} == --help ]]; then
    echo "Usage: bash build-sfpi.sh SFPI_SOURCE_DIR VERSION"
    echo "Build an already checked out SFPI release natively on Apple Silicon."
    echo "Optional: TT_BUILD_JOBS (default 8), TT_SFPI_GCC_PREFIX (Homebrew GCC prefix)."
    [[ ${1:-} == --help ]] && exit 0
    exit 2
fi
[[ $(uname -s) == Darwin && $(uname -m) == arm64 ]] || {
    echo "This recipe requires native Apple Silicon macOS." >&2; exit 1;
}
sfpi_dir=$(cd "$1" && pwd -P)
version=$2
[[ $sfpi_dir != *[[:space:]]* ]] || {
    echo "SFPI's upstream build requires a path without whitespace." >&2; exit 1;
}
[[ $(git -C "$sfpi_dir" rev-parse HEAD) == $(git -C "$sfpi_dir" rev-parse "$version^{commit}") ]] || {
    echo "SFPI checkout does not match requested release $version." >&2; exit 1;
}
for component in gcc binutils newlib; do
    expected=$(git -C "$sfpi_dir" rev-parse "HEAD:$component")
    actual=$(git -C "$sfpi_dir/$component" rev-parse HEAD)
    [[ $actual == "$expected" ]] || {
        echo "SFPI submodule $component does not match its pinned revision." >&2; exit 1;
    }
done

brew_prefix=$(brew --prefix)
gcc_prefix=${TT_SFPI_GCC_PREFIX:-$(brew --prefix gcc)}
gcc_bins=("$gcc_prefix"/bin/gcc-[0-9]*)
[[ ${#gcc_bins[@]} == 1 && -x ${gcc_bins[0]} ]] || {
    echo "Expected one Homebrew GCC driver under $gcc_prefix/bin." >&2; exit 1;
}
gcc_driver=${gcc_bins[0]}
gxx_driver=${gcc_driver%/gcc-*}/g++-${gcc_driver##*/gcc-}
[[ -x $gxx_driver ]] || { echo "Missing $gxx_driver" >&2; exit 1; }

for formula in make coreutils gnu-sed gawk; do
    export PATH="$brew_prefix/opt/$formula/libexec/gnubin:$PATH"
done
for formula in bison flex texinfo; do
    export PATH="$brew_prefix/opt/$formula/bin:$PATH"
done
export PATH="$brew_prefix/bin:$PATH"
for tool in bash make nproc sed awk bison flex makeinfo; do
    command -v "$tool" >/dev/null || { echo "Missing build tool: $tool" >&2; exit 1; }
done

# The compiler command sets the default; subconfigures can append another -std.
# In particular, libcody selects exactly C++11 and must be able to override it.
export CC="$gcc_driver -std=gnu11" CXX="$gxx_driver -std=gnu++17"
export CFLAGS="-O2" CXXFLAGS="-O2"
export CPPFLAGS="" LDFLAGS=""
for formula in gmp mpfr libmpc expat; do
    dependency_prefix=$(brew --prefix "$formula")
    export CPPFLAGS="$CPPFLAGS -I$dependency_prefix/include"
    export LDFLAGS="$LDFLAGS -L$dependency_prefix/lib"
done
export OMP_THREAD_LIMIT=${TT_BUILD_JOBS:-8}
[[ $OMP_THREAD_LIMIT =~ ^[1-9][0-9]*$ ]] || { echo "Invalid TT_BUILD_JOBS" >&2; exit 1; }
cd "$sfpi_dir"
exec bash scripts/build.sh --full "--version=$version"
