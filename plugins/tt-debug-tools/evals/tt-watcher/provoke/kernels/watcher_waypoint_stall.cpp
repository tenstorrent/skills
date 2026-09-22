// SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Walks two waypoints, then spins forever. Watcher's periodic dump reads the
// waypoint mailbox on each pass, so watcher.log ends up showing "STOP" as the
// last waypoint reached on this core. The literal is chosen so grep on
// watcher.log distinguishes this provoker from any other.

#include <cstdint>

#include "api/debug/dprint.h"
#include "api/debug/waypoint.h"

void kernel_main() {
    WAYPOINT("STRT");
    DPRINT("WATCHER_WAYPOINT_ABOUT_TO_STALL\n");
    WAYPOINT("STOP");
    while (true) { }
}
