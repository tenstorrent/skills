// SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Halts TRISC0 on an LLK assertion by initialising a copy for a circular buffer
// the unpacker is not configured for.
//
// compute_kernel_hw_startup points SrcA at cb_in0, whose format is Float16_b.
// An operation init assumes the unpacker already describes the buffer it is
// given; it does not reprogram the format. Initialising for cb_in1, which is
// Bfp8_b, therefore reaches the LLK pre-init self-check with SrcA still
// describing Float16_b, and the check ebreaks. The missing line is a
// reconfig_data_format_srca(cb_in0, cb_in1) immediately before the init.
//
// This is the shape behind most of the LLK-assert hangs seen in CI: the halt is
// one thread deep in an init, while the visible damage is dataflow cores waiting
// on buffers that will now never move.
//
// Nothing is read or written on purpose. The assert fires inside the init,
// before any tile is touched, so a data path would only add noise. The kernel
// returning normally means the assert did not fire, and the driver treats that
// as a failed capture rather than letting a healthy run be recorded as a hang.

#include <cstdint>

#include "api/compute/common.h"
#include "api/compute/tile_move_copy.h"
#include "api/dataflow/circular_buffer.h"
#include "api/debug/dprint.h"

void kernel_main() {
    constexpr auto cb_in0 = tt::CBIndex::c_0;   // Float16_b
    constexpr auto cb_in1 = tt::CBIndex::c_1;   // Bfp8_b
    constexpr auto cb_out = tt::CBIndex::c_16;  // Float16_b

    compute_kernel_hw_startup(cb_in0, cb_out);

    // The capture waits on this line. The halt lands microseconds later and is
    // permanent, so the marker means the device is stopped rather than still
    // building kernels. The trailing newline is load-bearing: an unterminated
    // print can sit in the buffer and never reach the file.
    DPRINT("LLK_ABOUT_TO_INIT_MISMATCHED_CB srca={} init_for={}\n", (uint32_t)cb_in0, (uint32_t)cb_in1);

    copy_tile_to_dst_init_short(cb_in1);
}
