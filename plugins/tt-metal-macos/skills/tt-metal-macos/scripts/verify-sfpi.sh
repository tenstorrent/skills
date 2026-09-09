#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
if [[ $# != 2 || ${1:-} == --help ]]; then
    echo "Usage: bash verify-sfpi.sh SFPI_INSTALL_DIR OUTPUT_DIR"
    [[ ${1:-} == --help ]] && exit 0
    exit 2
fi
sfpi_dir=$(cd "$1" && pwd -P)
mkdir -p "$2"
output_dir=$(cd "$2" && pwd -P)
script_dir=$(cd "$(dirname "$0")" && pwd -P)
compiler="$sfpi_dir/compiler/bin/riscv-tt-elf-g++"
file -L "$compiler"
file -L "$compiler" | grep -q 'Mach-O.*arm64'
"$compiler" --version
cc1plus=$("$compiler" -print-prog-name=cc1plus)
file -L "$cc1plus"
file -L "$cc1plus" | grep -q 'Mach-O.*arm64'
"$compiler" -std=c++17 -O2 -mcpu=tt-bh-tensix -fno-exceptions -fno-rtti \
    -I"$sfpi_dir/include" -c "$script_dir/../assets/sfpi-smoke.cpp" \
    -o "$output_dir/sfpi-smoke.o"
file "$output_dir/sfpi-smoke.o"
"$sfpi_dir/compiler/bin/riscv-tt-elf-readelf" -h "$output_dir/sfpi-smoke.o" \
    > "$output_dir/sfpi-smoke.elf-header.txt"
cat "$output_dir/sfpi-smoke.elf-header.txt"
grep -Eq 'Machine:.*RISC-V' "$output_dir/sfpi-smoke.elf-header.txt"
"$sfpi_dir/compiler/bin/riscv-tt-elf-objdump" -d "$output_dir/sfpi-smoke.o" \
    > "$output_dir/sfpi-smoke.disassembly.txt"
grep -Ei '[[:space:]]sfp(load|store|mul|mad|add)([[:space:]]|$)' \
    "$output_dir/sfpi-smoke.disassembly.txt"
