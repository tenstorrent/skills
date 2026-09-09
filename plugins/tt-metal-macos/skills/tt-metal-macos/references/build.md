# Build recipe

## Source pins and scope

The starting point is the upstream
[Apple Silicon port](https://github.com/tenstorrent/tt-metal/commit/df4808d78a99143a70dd7ec7011bb88d88ad2f27),
not current main. Its original UMD submodule commit is unavailable from the
configured upstream remote. Use the last published UMD revision from the port's
parent, then apply the bundled macOS patch.

| Component | Revision |
| --- | --- |
| tt-metal | `df4808d78a99143a70dd7ec7011bb88d88ad2f27` |
| UMD | `7b37f8aa1572806106683dfbcb096f465bef87fa` plus bundled patch |
| SFPI | `7.48.0` (`59543afa7e8f5cde8685cdc5510917bc0d86f393`, matching `tt_metal/sfpi-version`) |
| Public ttsim | `89bdc5eb726c4f1ebbe597e03b1b9cdf7622c779` |

The compiler target is `riscv-tt-elf`. Host binaries are Mach-O arm64; kernel
objects remain RISC-V ELF. The existing port's reported test counts are upstream
claims, not evidence that this machine passed those tests.

## Host dependencies

Use Xcode Command Line Tools and an Apple Silicon Homebrew installation. The
recipe needs native `cmake`, `ninja`, `bash`, `gcc`, `hwloc`, `coreutils`,
`gnu-sed`, `gawk`, `gmp`, `mpfr`, `libmpc`, `bison`, `flex`, `texinfo`, `make`,
`expat`, `autoconf`, `automake`, `libtool`, and `pkgconf`. Install missing
dependencies within the user's authorized scope:

```bash
HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 brew install \
  cmake ninja bash gcc hwloc coreutils gnu-sed gawk gmp mpfr libmpc \
  bison flex texinfo make expat autoconf automake libtool pkgconf
```

Build under a path without spaces; SFPI's upstream scripts and Makefiles do not
consistently quote paths. Use one build process per build directory and bound
parallelism to available memory; eight jobs were used for SFPI on an 18 GiB Mac.

## Prepare isolated sources

Set `skill_dir` to this skill's directory (containing `SKILL.md`) and `work_dir`
to a new build workspace. These commands create fresh checkouts; do not run them
over an existing user's checkout. For an existing workspace, first inspect it
and resume only the necessary stage.

```bash
mkdir -p "$work_dir"
git init "$work_dir/tt-metal"
git -C "$work_dir/tt-metal" remote add origin https://github.com/tenstorrent/tt-metal.git
git -C "$work_dir/tt-metal" fetch --depth 1 origin df4808d78a99143a70dd7ec7011bb88d88ad2f27
git -C "$work_dir/tt-metal" checkout --detach FETCH_HEAD
git -C "$work_dir/tt-metal" submodule update --init --depth 1 tt_metal/third_party/tracy

# Fetch the available UMD revision instead of the port's missing submodule pin.
umd_dir="$work_dir/tt-metal/tt_metal/third_party/umd"
git init "$umd_dir"
git -C "$umd_dir" remote add origin https://github.com/tenstorrent/tt-umd.git
git -C "$umd_dir" fetch --depth 1 origin 7b37f8aa1572806106683dfbcb096f465bef87fa
git -C "$umd_dir" checkout --detach FETCH_HEAD
git -C "$umd_dir" apply --check "$skill_dir/assets/umd-macos.patch"
git -C "$umd_dir" apply "$skill_dir/assets/umd-macos.patch"
git -C "$work_dir/tt-metal" apply --check "$skill_dir/assets/metal-macos.patch"
git -C "$work_dir/tt-metal" apply "$skill_dir/assets/metal-macos.patch"

git clone --depth 1 --branch 7.48.0 https://github.com/tenstorrent/sfpi.git "$work_dir/sfpi"
git -C "$work_dir/sfpi" submodule update --init --depth 1 --jobs 3
```

The UMD checkout will show as modified in its parent because it deliberately
uses a different published commit plus the patch. Do not run a blanket
`git submodule update` afterward; that would request the missing pin again.

## SFPI host compiler details

Use Homebrew GCC to build SFPI, keeping Apple Clang for the tt-metal host library.
Prepend GNU make, coreutils, sed, awk, bison, flex, and texinfo to `PATH` for SFPI.
Provide Homebrew GMP/MPFR/MPC/expat include and library directories through
`CPPFLAGS` and `LDFLAGS`.

With Homebrew GCC 16, put the language standard in the compiler command:

```bash
export CC="gcc-16 -std=gnu11"
export CXX="g++-16 -std=gnu++17"
export CFLAGS="-O2" CXXFLAGS="-O2"
```

Do not put `-std=gnu++17` in `CXXFLAGS`: SFPI's libcody subconfigure requires
exactly C++11 and appends `-std=c++11` to `CXX`. A later standard in `CXXFLAGS`
would override that selection. Leaving GCC 16's default standard unchanged also
breaks libcody's older UTF-8 string code.

Build the pinned source with `scripts/build.sh --full --version=7.48.0`, preserving
its pinned GCC, binutils, and newlib submodules. Use the resulting `build/sfpi`
directory as tt-metal's `runtime/sfpi`; keep version checking enabled.

The bundled helper selects the installed Homebrew GCC and sets these variables:

```bash
bash "$skill_dir/scripts/build-sfpi.sh" "$work_dir/sfpi" 7.48.0 \
  > "$work_dir/sfpi-build.log" 2>&1
bash "$skill_dir/scripts/verify-sfpi.sh" "$work_dir/sfpi/build/sfpi" \
  "$work_dir/sfpi-check"
mkdir -p "$work_dir/tt-metal/runtime"
ln -s "$work_dir/sfpi/build/sfpi" "$work_dir/tt-metal/runtime/sfpi"
```

## Metalium and simulator

Configure with `cmake/aarch64-apple-clang-toolchain.cmake`, Release, Ninja,
`ENABLE_DISTRIBUTED=OFF`, `WITH_PYTHON_BINDINGS=OFF`, `ENABLE_TRACY=OFF`, and
`BUILD_PROGRAMMING_EXAMPLES=ON`. Build selected Metalium targets first.

Avoid globally adding `/opt/homebrew/include`: an unrelated Homebrew `fmt`
version can shadow CPM's pinned headers and produce link failures. The bundled
patch resolves `hwloc.h` to its formula-specific include directory.

```bash
cmake -S "$work_dir/tt-metal" -B "$work_dir/tt-metal/build/macos" -G Ninja \
  -DCMAKE_TOOLCHAIN_FILE=cmake/aarch64-apple-clang-toolchain.cmake \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_COMPILE_WARNING_AS_ERROR=OFF \
  -DENABLE_DISTRIBUTED=OFF -DWITH_PYTHON_BINDINGS=OFF -DENABLE_TRACY=OFF \
  -DBUILD_PROGRAMMING_EXAMPLES=ON -DTT_UNITY_BUILDS=OFF
cmake --build "$work_dir/tt-metal/build/macos" --target tt_metal \
  metal_example_add_2_integers_in_riscv metal_example_eltwise_sfpu -j 4
```

Build the public simulator from its repository root:

```bash
git init "$work_dir/ttsim"
git -C "$work_dir/ttsim" remote add origin https://github.com/tenstorrent/ttsim.git
git -C "$work_dir/ttsim" fetch --depth 1 origin 89bdc5eb726c4f1ebbe597e03b1b9cdf7622c779
git -C "$work_dir/ttsim" checkout --detach FETCH_HEAD
cd "$work_dir/ttsim"
python3 make.py src/_out/release_bh/libttsim.so -j 4
```

Its `.so` suffix is conventional; inspect it with `file` to confirm it is Mach-O.
Place the Blackhole SOC descriptor beside a symlink to the simulator. Use
workspace-local cache and log directories so the validation is self-contained:

```bash
mkdir -p "$work_dir/sim"
ln -s "$work_dir/ttsim/src/_out/release_bh/libttsim.so" "$work_dir/sim/libttsim.so"
cp "$work_dir/tt-metal/tt_metal/soc_descriptors/blackhole_140_arch.yaml" \
  "$work_dir/sim/soc_descriptor.yaml"
export TT_METAL_HOME="$work_dir/tt-metal"
export TT_METAL_SIMULATOR="$work_dir/sim/libttsim.so"
export TT_METAL_SLOW_DISPATCH_MODE=1
export TT_METAL_CACHE="$work_dir/runtime-cache"
export TT_METAL_LOGS_PATH="$work_dir/runtime-logs"
cd "$TT_METAL_HOME"
./build/macos/programming_examples/metal_example_add_2_integers_in_riscv
./build/macos/programming_examples/metal_example_eltwise_sfpu
```

The bundled UMD changes preserve the Linux implementation and enable the
process-local simulator path on macOS. Linux memfd-based library copies and
process-shared robust hardware locks fail explicitly if requested on macOS.
This is not a physical-device driver port.

## Observed validation

On macOS 26.6.2 arm64, using Homebrew GCC 16.1.0 and Apple Clang 21.0.0:

- The complete SFPI 7.48.0 source build finished successfully. The packaged
  build helper also completed against that installation on an incremental run.
- The packaged SFPI check identified both `riscv-tt-elf-g++` and `cc1plus` as
  Mach-O arm64. Its Blackhole vector kernel produced a RISC-V ELF32 object
  containing `sfpload`, `sfpmad`, and `sfpstore` instructions.
- Patched UMD and the public Blackhole simulator built as Mach-O arm64 dylibs.
  A normal UMD write/read round-trip through simulator L1 memory passed.
- `tt_metal`, `metal_example_add_2_integers_in_riscv`, and
  `metal_example_eltwise_sfpu` compiled and linked as Mach-O arm64.
- The integer example ran successfully from an initially absent kernel cache,
  reported `Success: Result is 21`, and recorded 0/9 JIT cache hits. The SFPU
  exponential example then reported `Test Passed` for its 64 tiles of input.
- These runs did not validate TTNN, Python bindings, other simulator
  architectures, hardware execution, or performance.

## Troubleshooting

- **`aarch64_debian` executable fails on macOS:** it is a Linux executable, not
  a Darwin compiler. Build matching SFPI sources.
- **Unavailable UMD commit:** the original port references
  `c7aac4cbc1c305f23daf197516d085d76f5fe96c`. Use the published pin and patch above.
- **`/tmp/build_sfpi.sh` in a CMake error:** that path is a leftover from the
  upstream port author's machine; build SFPI using this recipe instead.
- **`C++11 is required` in libcody:** check the ordering of `CXX` and `CXXFLAGS`
  described above, then reconfigure the affected build stage.
- **Missing target `<cstdint>` after SFPI's driver exists:** finish the full
  SFPI build. The stage-one compiler can run before stage two installs the
  RISC-V C++ headers and libraries.
- **GNU/BSD shell utility differences:** use the scoped GNU tool `PATH`, rather
  than modifying global shell configuration.
- **`nice: cannot set niceness` in a sandbox:** first check whether the command
  actually exited; some implementations continue. Do not start another build
  in that directory while the first process is still active.
- **Inspector RPC bind or shared-memory statistics warnings in a sandbox:**
  both examples passed despite those optional diagnostics being unavailable.
  For a sandboxed run that does not need them, the port provides
  `TT_METAL_INSPECTOR_RPC=0` and `TT_METAL_SHM_TRACKING_DISABLED=1`.

Report the targets actually compiled and examples actually executed. Native
SFPI, native Metalium, TTNN, Python bindings, and hardware execution are distinct
claims.
