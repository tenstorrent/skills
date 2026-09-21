// SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Parks BRISC in noc_async_atomic_barrier() permanently.
//
// noc_semaphore_inc_multicast is non-posted, so each call adds num_dests to
// this core's software acked-counter target and the barrier spins until the
// hardware has returned that many atomic responses. Only the real destinations
// ever respond. Declaring PHANTOM_DEST_CORES more than exist therefore leaves a
// deficit of exactly NUM_ITERS * PHANTOM_DEST_CORES, and the barrier never
// exits.
//
// The deficit is the point: it is the arithmetic a reader has to do to get from
// the parked frame to the cause, so it is kept exact rather than approximate.

#include <cstdint>

#include "api/dataflow/dataflow_api.h"
#include "api/debug/dprint.h"

void kernel_main() {
    constexpr uint32_t semaphore_id = 0;
    const uint32_t semaphore_addr = get_semaphore(semaphore_id);

    // This kernel runs only on logical (0,0), which is the virtual tensix
    // origin, and the rectangle starts one column over: a non-loopback
    // multicast cannot target its own sender.
    constexpr uint32_t start_x = VIRTUAL_TENSIX_START_X + 1;
    constexpr uint32_t start_y = VIRTUAL_TENSIX_START_Y;
    constexpr uint32_t end_x = VIRTUAL_TENSIX_START_X + GRID_X - 1;
    constexpr uint32_t end_y = VIRTUAL_TENSIX_START_Y + GRID_Y - 1;

    constexpr uint32_t real_dest_cores = (GRID_X - 1) * GRID_Y;

    const uint64_t mcast_addr = get_noc_multicast_addr(start_x, start_y, end_x, end_y, 0) | semaphore_addr;

    for (uint32_t i = 0; i < NUM_ITERS; ++i) {
        noc_semaphore_inc_multicast(mcast_addr, 1, real_dest_cores + PHANTOM_DEST_CORES);
    }

    // The capture waits on this line rather than on a host-side timer: a cold
    // JIT build takes minutes and the barrier is entered microseconds after the
    // print, so this is the only signal that means the device is actually stuck
    // rather than still compiling. The trailing newline is load-bearing — an
    // unterminated print can sit in the buffer and never reach the file.
    DPRINT("MCAST_AT_BARRIER outstanding={}\n", NUM_ITERS * PHANTOM_DEST_CORES);

    noc_async_atomic_barrier();
}
