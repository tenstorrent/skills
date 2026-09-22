// SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Writes a distinctive magic word to a fixed L1 offset on BRISC and spins.
// The address and value are compile-time and named in the eval prompt, so
// the agent's job is to use tt-exalens to read the same address and report
// the value.

#include <cstdint>

#include "api/debug/dprint.h"

// MAGIC_ADDR and MAGIC_VALUE come from the driver's `defines=[...]`.

void kernel_main() {
    volatile uint32_t* p = reinterpret_cast<volatile uint32_t*>(MAGIC_ADDR);
    *p = MAGIC_VALUE;
    DPRINT("EXALENS_MAGIC_WRITTEN\n");
    while (true) { }
}
