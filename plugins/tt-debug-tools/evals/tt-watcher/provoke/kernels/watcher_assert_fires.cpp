// SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Trips watcher's assert reporter. Under WATCHER_ENABLED the ASSERT macro
// expands to assert_and_hang(__LINE__): the line number lands in watcher's
// mailbox and the core spins forever. Watcher's server prints the line and
// the core coord to stderr and to watcher.log.

#include <cstdint>

#include "api/debug/dprint.h"
#include "api/debug/assert.h"

void kernel_main() {
    DPRINT("WATCHER_ASSERT_ABOUT_TO_FIRE\n");
    ASSERT(0);
}
