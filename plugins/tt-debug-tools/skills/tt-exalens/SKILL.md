---
name: tt-exalens
description: Read and write one address, register or core on a Tenstorrent device with tt-exalens — L1 and DRAM reads, RISC-V registers and GPRs, tensix state, NoC registers, callstacks from an ELF, memory search, and a GDB server for stepping a RISC. Use for a specific question about a specific location, when tt-triage's fixed script set does not ask it, or when you need JTAG or GDB. For the whole-device sweep, that is tt-triage.
metadata:
  tier: process
  upstream:
    - repo: tenstorrent/tt-exalens
      ref: 04c02f2626fe6380a18fb1a3be1ca6b17459de42
      path: ttexalens
---

# tt-exalens

The low-level hardware debugger everything else here stands on. `tt-triage` is a
collection of Python scripts over this; when a triage script does not ask your
question, this is where you ask it directly.

Verified against tt-exalens **0.3.27** — the version `tt-triage` pins, and it
refuses on a mismatch.

## When to invoke

- You need one address on one core, not a sweep: an L1 word, a semaphore, a
  register.
- You want a callstack for a specific RISC against a specific ELF.
- You want to step a RISC under GDB, or reach a board over JTAG.
- Triage's scripts do not cover the question, and writing a new triage script is
  more than the question is worth.

Not this skill: the whole-device pass with interpretation — `tt-triage`. Reading
watcher structures — `tt-watcher`.

## Surface

Four modes:

```bash
tt-exalens                                    # interactive
tt-exalens --commands="d; brxy 0,0 0x0 16; x" # batch, semicolon-separated
tt-exalens --server --port=5555               # serve, for --remote or tt-triage
tt-exalens --gdb [gdb_args...]                # RISC-V gdb client
```

| Option | Effect |
|---|---|
| `--commands=<cmds>` | Run semicolon-separated commands and exit. |
| `--server`, `--port=<port>` | Start a server; default port 5555. |
| `--remote`, `--remote-address=<ip:port>` | Attach to one; defaults to `localhost:5555`. |
| `--start-server=<port>`, `--start-gdb=<port>` | Start either alongside an interactive session. |
| `--background` | Detach the server; exit by creating an `exit.server` file. |
| `--jtag` | Initialise the JTAG interface instead of PCIe. |
| `--noc-id=<id>` | `0`/`NOC0`, `1`/`NOC1`, `2`/`SYSTEM_NOC`. **Default 1.** |
| `--disable-noc-failover` | Stop automatic NOC0↔NOC1 failover. |
| `--unsafe-mode` | Permit writes to regions otherwise refused. |
| `--test` | Non-zero exit on any exception — the flag for scripting. |
| `--verbosity=<1-5>` | ERROR through DEBUG; default 3. |

Command set and argument shapes: `references/commands.md`. Server, GDB and JTAG:
`references/server-and-gdb.md`.

## Force the state

Any live or idle device answers a read. To confirm the tool and the device agree:

```bash
tt-exalens --commands="device; exit" --test
```

For something worth reading, halt a core first — the sanitize case in
`tt-watcher`'s Force the state — then attach and take a callstack.

## Output

Per command, to the console. `--commands` echoes each one first:

```
Executing command: help
Full Name          Short    Description
burst-read-xy      brxy     Reads a block of data at <addr> on <noc-loc>...
callstack          bt       Prints callstack using provided elf for a given RiscV core.
```

Reads print the requested words; `device` prints a per-device RISC-V summary.
Every command has a short alias, and the aliases are what triage scripts and
existing notes use — `brxy`, `bt`, `gpr`, `reg`, `nc`, `re`.

## Traps

**`--noc-id` defaults to 1, not 0.** A read that disagrees with another tool's
view may simply be arriving over the other NoC. State it explicitly when it
matters.

**NOC failover is automatic and silent.** If communication fails on one NoC it
switches to the other, so a read can succeed over a path you did not choose —
which hides exactly the wedged-NoC condition you may be trying to confirm. Use
`--disable-noc-failover` when the question is *whether* a NoC works.

**Triage enforces the version pin and refuses on a mismatch.** Do not work around
it with `--skip-version-check` without owning what follows: the scripts read
structures whose layout the pin is there to guarantee.

**A server is required for `tt-triage --remote-exalens`**, and it has to be
started *before* the workload takes the device. Started afterwards, it cannot
open the device either.

**It writes as readily as it reads.** `write`, `write-xy` and `run-elf` change
device state, and `--unsafe-mode` removes the guard that refuses the dangerous
regions. On a shared host that is someone else's run.

**Halting to read changes what you are reading.** The same caveat triage carries:
inspection is not free, and a core halted for a read is a core that is no longer
where it was.
