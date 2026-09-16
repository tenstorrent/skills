# Server, GDB and JTAG

Three ways to reach a device that the plain CLI cannot.

## The server

A tt-exalens server owns the device; clients talk to it instead of opening the
device themselves. Two reasons to want one:

1. **The device is already owned.** UMD init fails when another process holds it,
   so nothing new can open it — including `tt-triage`. A server started *first*
   is the way in.
2. **Remote debugging.** The board is on one host, you are on another.

```bash
tt-exalens --server --port=5555              # foreground
tt-exalens --server --background             # detached
tt-exalens --remote --remote-address=host:5555
```

`--remote-address` accepts `:port` alone when the host is localhost. Default is
`localhost:5555` on both sides.

A `--background` server does not exit on ENTER. It exits when an `exit.server`
file is created — so a forgotten background server keeps holding the device, and
is exactly the kind of leftover holder that makes every later run fail to
initialise.

`--start-server=<port>` runs one alongside an interactive session, rather than
instead of it.

### Pairing with tt-triage

`tt-triage --remote-exalens` talks to this server. The ordering is the whole
trick and it is easy to get wrong:

1. Start `tt-exalens --server` **before** the workload.
2. Start the workload.
3. When it hangs, run `tt-triage --remote-exalens`.

Started after the workload has the device, the server cannot open it either, and
you are left with no path in. This is the only route to triaging a device you
cannot open, so the ordering has to be decided before the run rather than after
the hang.

## GDB

```bash
tt-exalens --start-gdb=<port>    # server, alongside a session
tt-exalens --gdb [gdb_args...]   # the RISC-V gdb client
```

Or `gdb` from inside a session, which starts and stops the server.

This is what a halted core is *for*: a lightweight kernel assert leaves the core
in debug mode rather than dead, and where a callstack is not enough, GDB steps
it. `tt-asserts` puts a core there; this reaches it.

The client is a RISC-V GDB — the target is the core, not the host process. For
host-side debugging of the Metal runtime, ordinary GDB on the process is the
tool, and `tt-watcher`'s `tt::watcher::dump` is what you call from it.

## JTAG

```bash
tt-exalens --jtag
```

Initialises the JTAG interface instead of PCIe. The reason to want it: PCIe is
not always available. A board whose PCIe link is down, or whose ARC is
unresponsive to the usual path, may still answer over JTAG — so this is the last
resort before calling a board dead.

Nothing else in this plugin has a JTAG path.

## NoC selection, and why failover hides things

`--noc-id` takes `0`/`NOC0`, `1`/`NOC1`, or `2`/`SYSTEM_NOC`, and **defaults to
1**. Failover between NOC0 and NOC1 is automatic in both directions.

That is helpful when you want an answer and unhelpful when the question *is*
which NoC works. A read that succeeds may have arrived over the other NoC
without saying so:

```bash
tt-exalens --noc-id=0 --disable-noc-failover --commands="device; exit" --test
```

is how you establish that NOC0 specifically is alive — which is the same
condition `tt-triage --initialize-with-noc1` exists to work around from the other
side.
