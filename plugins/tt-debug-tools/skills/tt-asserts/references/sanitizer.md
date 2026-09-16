# LLK sanitizer

Deeper invariant checking inside tt-llk, reported by severity rather than by
halting. Source of truth is `tt-metal/tt_metal/tt-llk/common/sanitizer/output.h`
and the `EnvVarID` list in `tt_metal/llrt/rtoptions.cpp`.

**There is no documentation page for this feature.** Everything below is read
from those two files; nothing here comes from a doc, because upstream does not
ship one. That is a genuine gap, not an omission in this skill.

## Enabling

```bash
export TT_METAL_LLK_SANITIZER=1
```

The build then emits `-DLLK_SAN_ENABLE`. Six severities, each an **independent
switch** rather than a threshold:

| Variable | Trigger |
|---|---|
| `TT_METAL_LLK_SANITIZER_PEDANTIC=1` | `pedantic` |
| `TT_METAL_LLK_SANITIZER_WARN=1` | `warn` |
| `TT_METAL_LLK_SANITIZER_ERROR=1` | `error` |
| `TT_METAL_LLK_SANITIZER_FAULT=1` | `fault` |
| `TT_METAL_LLK_SANITIZER_INFO=1` | `info` |
| `TT_METAL_LLK_SANITIZER_INTERNAL=1` | `internal` |

Setting `ERROR` does not imply `FAULT`, and enabling the sanitizer without any
severity gives you the instrumentation and no reports. Enable the ones you want
explicitly.

## It will not compile alone

`LLK_SAN_ENABLE` requires either `ENABLE_LLK_ASSERT` or `DEBUG_PRINT_ENABLED`.
With neither, the build stops:

```
llk::san | fault   | LLK_SAN_ENABLE is set but neither ENABLE_LLK_ASSERT nor DEBUG_PRINT_ENABLED is defined
```

A compile-time `#error`, so this fails loudly rather than degrading — which is
the opposite of most of the traps in this plugin, and worth knowing so the build
failure is read as a missing pairing rather than a broken checkout. In practice:
enable LLK asserts, or enable device print, alongside it.

`DEBUG_PRINT_ENABLED` is also rejected inside LLK infra specifically:

```
llk::san | fault   | DEBUG_PRINT_ENABLED is not supported in LLK INFRA, only in metal
```

## Output

Unlike the other two families the sanitizer **prints** rather than halting. Lines
are prefixed with the namespace and the severity, padded to a fixed width:

```
llk::san | error    | <message>
llk::san | pedantic | <message>
```

It reports through the device print path, which is why the compile-time pairing
above exists — the strings are `CTSTR` literals resolved by the host, the same
mechanism `tt-dprint` documents.

**Do not infer an ordering from the severities.** The names are `Trigger` enum
values and the padding widths are cosmetic; nothing in the source states which
class of invariant maps to `error` versus `fault` versus `internal`. The
`llk::san` call sites in `tt_metal/tt-llk/` are where that would be settled.
