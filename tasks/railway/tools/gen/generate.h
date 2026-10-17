#pragma once

#include <cstdint>

#include "config.h"
#include "workload.h"
#include "world.h"

namespace railtools {

// Both generators are deterministic functions of (config, seed, inputs) and
// use integer arithmetic only, so output is byte-identical across platforms.
World generate_world(Config& cfg, uint64_t seed);
Workload generate_workload(const World& world, Config& cfg, uint64_t seed);

}  // namespace railtools
