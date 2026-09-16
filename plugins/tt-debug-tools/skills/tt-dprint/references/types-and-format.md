# Types and format strings

`DPRINT` takes an `fmt`-style format string. The stream form
(`DPRINT << x << ENDL()`) is gone; the operators and the `BF16`, `F32`, `U32`,
`HEX`, `SETPRECISION`, `FIXED`, `DEFAULTFLOAT` and `ENDL` helpers remain only as
`[[deprecated]]` stubs. Do not write new call sites in that style and do not
copy examples that use it.

```c++
#include "api/debug/dprint.h"   // required in every kernel that prints
```

## Scalars

`bool` (prints `false` / `true`), `char`, every fixed-width integer type
(`uint8_t`–`uint64_t`, `int8_t`–`int64_t`), `float`, `double`.

```c++
DPRINT("Test string {} {} {}\n", 'a', 5, 0.123456f);
DPRINT("Bool value: {}\n", true);          // Bool value: true
```

bfloat16 has a dedicated type rather than a manipulator:

```c++
bf16_t v(0x3dfb);                          // 0.122559
DPRINT("BF16 value: {}\n", v);
```

## Format specifiers

Whatever fmtlib supports, including positional arguments reused with different
specs:

```c++
DPRINT("{:.5f}\n", 0.123456f);             // fixed precision
DPRINT("{:>10}\n", 123);                   // right align, width 10
DPRINT("{:<10}\n", 123);                   // left align
DPRINT("{0:x} {0} {0:o} {0:b}\n", 15);     // hex, decimal, octal, binary
```

## Strings

A runtime `const char*` prints as **an address**, not text — the host has no way
to read device memory. `CTSTR()` puts the literal in the ELF at compile time so
the host can resolve it:

```c++
const char* s = "Hello world!";
DPRINT("Pointer: {}\n", s);                // Pointer: 0x12345678
DPRINT("String: {}\n", CTSTR("Hello!"));   // String: Hello!
```

## Enums

Printed as symbolic names **when the ELF carries DWARF debug info**, and as
`(TypeName)integer` when it does not — so the same kernel prints differently
between build types, and a numeric enum in a log is a build artefact rather than
a hint about the value.

```c++
enum class Color : uint8_t { Red = 0, Green = 1, Blue = 2 };
DPRINT("Color: {}\n", Color::Green);       // Color: Green
DPRINT("Color: {:#}\n", Color::Blue);      // Color: Color::Blue   (qualified)
```

Enums with an `operator|` are detected at compile time and printed with `|`
separators: `Flags: A | C`, or `Flags: Flags::A | Flags::C` under `{:#}`.

## Per-RISC macros

Each emits only on its own RISC, so the same kernel source can print different
things from each:

| Macro | Emits on |
|---|---|
| `DPRINT` | Whichever RISC is executing |
| `DPRINT_UNPACK` | Unpacker |
| `DPRINT_MATH` | Math |
| `DPRINT_PACK` | Packer |
| `DPRINT_DATA0` | Data movement, noc 0 |
| `DPRINT_DATA1` | Data movement, noc 1 |

## The newline rule

Every format string ends with `\n`. The host server splits each per-RISC stream
on newlines and buffers anything else; a trailing partial line is dropped at
device detach rather than flushed.

```c++
DPRINT("hit checkpoint {}\n", id);   // printed
DPRINT("hit checkpoint {}", id);     // may never be printed
```

This is upstream's own stated first suspect when prints do not appear.
