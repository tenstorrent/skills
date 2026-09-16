// SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Corrupts this core's mailbox magic, then parks forever on a circular buffer
// nothing will ever fill.
//
// The pairing is the point. Triage reports the magic mismatch and a callstack
// for the same core, and the skill's read order says a corrupt magic makes every
// mailbox-derived reading — that callstack included — untrustworthy. A reader
// who takes the parked frame at face value has been caught by the trust gate.
//
// The write is a local four-byte store to this core's own L1, not a NoC write to
// a neighbour. It reaches the same triage output with no coordinate arithmetic
// that could resolve onto an ethernet core, where the corrupted state would be
// live inter-chip routing rather than a field only host tooling reads. The
// offset comes from the struct definition rather than a literal, so it cannot
// drift away from the layout the firmware and triage agree on.

#include <cstdint>

#include "api/dataflow/dataflow_api.h"
#include "api/debug/dprint.h"
#include "hostdev/dev_msgs.h"

void kernel_main() {
    constexpr uint32_t cb_never_filled = 0;

    volatile tt_l1_ptr mailboxes_t* const mailboxes = (tt_l1_ptr mailboxes_t*)(MEM_MAILBOX_BASE);
    mailboxes->core_info.core_magic_number = static_cast<CoreMagicNumber>(CORRUPT_MAGIC);

    // Printed after the store and before the park, so the marker means both have
    // happened. The trailing newline is load-bearing: an unterminated print can
    // sit in the buffer and never reach the file.
    DPRINT("CORE_MAGIC_CORRUPTED then_parking=1\n");

    // No other kernel is in this program, so nothing ever pushes. The wait is
    // absorbing, which is what keeps the process alive and inspectable.
    cb_wait_front(cb_never_filled, 1);
}
