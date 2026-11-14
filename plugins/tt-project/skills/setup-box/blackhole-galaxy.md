# Blackhole Galaxy hardware reference

What a Blackhole Galaxy system (Quanta S7TK, 6U) is built from, and how PCIe and power reach each
chip. Read this before naming a part in a diagnosis, a repair step or a post. Sources: the
system's product specification and PDB schematic, and Tenstorrent system-engineering notes.

## Field-replaceable units

- Tray (UBB): 4 per box, 8 chips each.
- 1 cable cartridge (CC).
- 1 motherboard (MB) module: AMD SP5 CPU and an AST2600 BMC.
- 1 PDB (power distribution board).
- 4 PSUs.
- 1 set of two bus bars.
- Fans.

There is no CC or bus bar per tray or slot: one of each serves the whole box. Say "MB module", not
"head node". "Linking board" is a Wormhole term; never use it for Galaxy.

## Tray

- 8 chips, 1 CPLD, 14 QSFP-DD ports.
- Tray 1 to tray 4 sit on PCIe buses `0x`, `4x`, `cx` and `8x`.

## PCIe, per chip

- Every chip has its own PCIe link and root port. Per tray, one chip runs at x8 and seven at x1
  (4 at x8 and 28 at x1 per box). Rev A/B boxes run Gen4, Rev C Gen5.
- Path: CPU root port (MB module) -> MB trace -> retimer on the PDB -> the CC (that tray's PCIe
  ExaMAX connector) -> chip.
- The retimers are part of the PDB and are replaced only with it. Repair steps never name a
  "retimer cable" or a separate retimer part. A PCIe cable between the MB module and the PDB is
  not confirmed, so never name one either.
- Known Gen5 issue: an extra AC coupling capacitor on the MB's CPU TX -> retimer RX trace can wedge
  Gen5 links after a reset; Gen4 does not reproduce it. The fix is an MB rework, so Gen5 link drops
  right after a reset point at the MB module, not at the tray or the CC.

## Cable cartridge (CC)

- Per tray, 4 ExaMAX connectors: 3 for tray-to-tray Ethernet, 1 for PCIe.
- There are no direct tray 1-tray 4 or tray 2-tray 3 links through the CC; QSFP cables add more
  links.
- Which sideband or management signals the CC carries is not documented: don't assume any.
- No power goes through the CC.

## Power

- Path: 4 PSUs (4 kW each, 54 V out, IEC C20 inlets, 3+1 redundant) -> PDB -> 8 54V-to-12V DC-DC
  modules (U1-U8) -> 12 V over the bus bars -> each tray's hot-swap controllers (HSC, MP5990) ->
  chips.
- The DC-DC modules are what the specification also calls the "54V power module" or "brick". BMC
  sensors `TEMP_PWRBRK0`-`TEMP_PWRBRK7` read them (upper-critical 90 C). They are part of the PDB,
  not separately replaceable. In posts say "the PDB's 54-to-12 V DC-DC modules (power bricks)".
- Maximum draw per the public guide: 14.2 kW. A C20 inlet at 16 A/230 V caps each PSU at 3.68 kW
  input.

## Related

- Resets, power cycles, tray drops and tenants: `recovery.md`.
