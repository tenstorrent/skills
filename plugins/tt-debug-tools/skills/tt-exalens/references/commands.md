# Command set

Every command has a full name and a short alias. The aliases are what triage
scripts and existing notes use, so both are worth recognising.

## Reading and writing memory

| Command | Alias | Does |
|---|---|---|
| `read` | `r` | Read a block at an address |
| `burst-read-xy` | `brxy` | Read a block at `<addr>` on `<noc-loc>`, or the current location when omitted |
| `write` | `w` | Write a block at an address |
| `write-xy` | `wxy` | Write a word at `<addr>` on `<noc-loc>`, or the current location |
| `search-memory` | `search` | Search device memory for a byte pattern |

`<noc-loc>` is a NoC location like `0,0`. Omitting it uses whatever `go` last
set, which is convenient interactively and a trap in a `--commands` batch — name
the location explicitly there.

## Registers and core state

| Command | Alias | Does |
|---|---|---|
| `dump-gpr` | `gpr` | All RISC-V registers for BRISC, TRISC0, TRISC1, TRISC2 on the current core |
| `tensix-reg` | `reg` | Read or write a named register at a location |
| `dump-tensix-state` | `tensix` | A named group of tensix state |
| `debug-bus` | `dbus` | RISC-V debug bus |
| `riscv` | `rv` | RISC-V debug commands — halt, step, resume |
| `perf-counters` | `pcnt` | Read and control the hardware performance counters on a worker core |

## Callstacks and ELFs

| Command | Alias | Does |
|---|---|---|
| `callstack` | `bt` | Callstack for a given RISC-V core, using a provided ELF |
| `run-elf` | `re` | Load an ELF into a BRISC and run it |
| `dump-coverage` | `cov` | Extract gcda coverage data for an ELF from a core |

`callstack` needs the ELF that was actually loaded, which is why Inspector's
kernel paths matter: without the right ELF the symbols do not resolve, and a
callstack of addresses is much less useful than one of names.

`run-elf` writes and executes. It is not a diagnostic.

## Navigation and topology

| Command | Alias | Does |
|---|---|---|
| `device` | `d` | Device summary; with no argument, RISC-V status for every device |
| `go` | | Set the current device, location and NoC |
| `noc` | `nc` | NoC registers |

`device` is the first thing to run — it confirms the tool sees the hardware and
prints the per-RISC state that tells you which cores are worth looking at.

## Session

| Command | Alias | Does |
|---|---|---|
| `server` | | Start or stop a tt-exalens server |
| `gdb` | | Start or stop a GDB server |
| `help` | `h` | Summary; `-v` for detail; a command name for just that one |
| `reload` | `rl` | Reload the `cli_commands` directory, for command development |
| `exit` | `x` | Exit, with an optional exit code |

## Batching

```bash
tt-exalens --commands="device; go 0,0; brxy 0,0 0x0 16; exit" --test
```

`--test` is what makes this scriptable: without it an exception still exits zero,
so a failed read looks like a successful run with empty output. Pair it with
`--verbosity=1` to keep the log out of the way, or `5` when the read itself is
what is suspect.

Use `help -v` on the device for argument shapes — they are versioned with the
tool, and this table deliberately names commands rather than pinning their
arguments.
