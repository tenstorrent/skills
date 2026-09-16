// SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
// SPDX-License-Identifier: Apache-2.0
//
// A multicast write followed by a multicast semaphore increment with no
// noc_async_write_barrier between them — the canonical missing-barrier
// pattern the NoC debug dump summary flags. The kernel completes; the
// dump prints its finding at device close.

#include <cstdint>

#include "api/dataflow/dataflow_api.h"

void kernel_main() {
    constexpr uint32_t semaphore_id = 0;
    const uint32_t semaphore_addr = get_semaphore(semaphore_id);

    // Multicast to every worker on the grid; the exact set does not matter,
    // only that the write goes out unflushed before the sema inc.
    const uint64_t mcast_dst = get_noc_multicast_addr(0, 0, GRID_X - 1, GRID_Y - 1, semaphore_addr);
    const uint32_t num_dests = GRID_X * GRID_Y;

    // Bogus but plausible source and payload; the dump only cares that a
    // write left BRISC without a barrier before the semaphore inc.
    constexpr uint32_t src_l1 = 0x60000;
    volatile uint32_t* p = reinterpret_cast<volatile uint32_t*>(src_l1);
    *p = 0xA5A5A5A5;

    noc_async_write_multicast(src_l1, mcast_dst, 4, num_dests);
    // Missing: noc_async_write_barrier().
    noc_semaphore_inc(mcast_dst, 1);
}
