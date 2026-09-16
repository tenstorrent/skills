// SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Trips watcher's NoC sanitize check with a write to a virtual coordinate that
// does not map to any Tensix/Ethernet/DRAM/PCIE core. Under
// TT_METAL_WATCHER=1 the compiled sanitizer wrapper catches the bad address
// before it reaches the NoC, writes the fault to the watcher mailbox, and the
// core halts — the workload then hangs on the never-completing write. Watcher's
// server prints the fault line to stderr and lands the same in watcher.log.
//
// Virtual (26, 18) is what MeshWatcherFixture.TensixTestWatcherSanitize uses;
// this kernel reuses that coordinate so the fault line reads familiar.

#include <cstdint>

#include "api/dataflow/dataflow_api.h"
#include "api/debug/dprint.h"

void kernel_main() {
    constexpr uint32_t src_l1 = 0x155000;
    constexpr uint32_t dst_l1 = 0x00123000;
    // Prints on the line before the sanitize-guarded call. In a hang capture
    // this is what tells the harness the core is stuck rather than still
    // building the kernel.
    DPRINT("WATCHER_NOC_SANITIZE_ABOUT_TO_FIRE\n");
    uint64_t dst = get_noc_addr(26, 18, dst_l1);
    noc_async_write(src_l1, dst, 4);
    noc_async_write_barrier();
}
