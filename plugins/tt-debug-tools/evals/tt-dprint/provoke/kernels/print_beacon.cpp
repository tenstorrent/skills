// SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
// SPDX-License-Identifier: Apache-2.0
//
// One-shot beacon on BRISC. When DPRINT is enabled for this core the DPRINT
// server captures the literal below; when it is not, the kernel runs and
// nothing lands anywhere. The pair — a specific line back, and silence
// otherwise — is what the eval grades.

#include <cstdint>

#include "api/debug/dprint.h"

void kernel_main() {
    DPRINT("TT_DPRINT_EVAL_BEACON=0x{:x}\n", 0xC0FFEEu);
}
