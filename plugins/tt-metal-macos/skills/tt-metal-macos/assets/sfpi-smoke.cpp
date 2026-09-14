// SPDX-License-Identifier: Apache-2.0
namespace ckernel {
unsigned int* instrn_buffer;
}
#include <sfpi.h>

void sfpi_smoke() {
    sfpi::vFloat value = sfpi::dst_reg[0];
    sfpi::dst_reg[1] = value * value + 1.0f;
}
