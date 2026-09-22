// SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
// SPDX-License-Identifier: Apache-2.0
//
// A witness kernel whose filename is distinctive enough that a diagnosis
// containing it had to have come from reading Inspector's kernel record.
// The kernel does nothing else — Inspector records the kernel path when
// the program is built, and the agent queries that path from a hung run.

#include <cstdint>

#include "api/debug/dprint.h"

void kernel_main() {
    DPRINT("INSPECTOR_WITNESS_KERNEL_ABOUT_TO_HANG\n");
    while (true) { }
}
