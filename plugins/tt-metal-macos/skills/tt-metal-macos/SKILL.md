---
name: tt-metal-macos
description: Build tt-metal and its matching SFPI compiler natively on Apple Silicon macOS, and validate kernels with the public ttsim simulator. Use for Mac build setup and Darwin toolchain troubleshooting.
---

# Native tt-metal on Apple Silicon

Build a reproducible, pinned macOS development environment. Read
[the build recipe](references/build.md) before starting; it records the source
revisions, required portability patches, commands, and validation boundaries.

## Choose the correct host toolchain

ARM64 describes the CPU, not the operating system. SFPI's `aarch64_debian`
release assets are Linux ELF executables; macOS needs Mach-O arm64 executables.
SFPI still **outputs RISC-V ELF** firmware and Tensix kernels on either host.
Inspect the compiler with `file` and `--version`, rather than inferring support
from an archive's architecture label.

Use the SFPI version pinned by the selected tt-metal checkout. Build its
Tenstorrent GCC/binutils sources when a matching Darwin release is unavailable.
A generic Homebrew RISC-V compiler lacks Tenstorrent extensions. Do not bypass
the SFPI version check to make configuration succeed.

## Establish scope and preserve the checkout

Inspect the existing checkout, modifications, submodules, host OS, and available
build tools. Use a separate checkout for the pinned recipe. It uses an upstream
macOS port that diverges from main; do not silently substitute it when the task
requires the user's current revision. In that case, use this recipe as a porting
reference and build the requested revision separately.

Keep Homebrew host dependencies separate from the RISC-V target libraries. Use
Apple Clang/libc++ for Metalium and Homebrew GCC/libstdc++ for building SFPI.
The recipe sets a host C++ standard explicitly for newer Homebrew GCC versions.

The simulator runs natively without Docker or a Linux VM. Hardware driver paths
and process-shared hardware locks remain unsupported on macOS. Do not turn them
into successful no-ops or present simulator results as physical-device results.

## Verify the result

Treat configuration, compilation, loading, and execution as separate evidence:

1. Check that SFPI's driver and internal compiler programs are Mach-O arm64.
2. Compile a small SFPI vector kernel and inspect its RISC-V object/disassembly.
3. Build and inspect the Metalium dylib and selected programming examples.
4. Build the public Blackhole simulator and execute an example with slow dispatch.

Record exact source SHAs, patches, commands, versions, and observed results. If
TTNN, Python bindings, another architecture, or hardware tests were not built or
run, say so. For failures, keep the first useful diagnostic and build log; repair
the narrow cause and resume the affected stage. Check whether a process exited
before launching another build in the same directory.
