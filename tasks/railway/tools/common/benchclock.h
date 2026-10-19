#pragma once
#include <chrono>
#include <cstdint>
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#endif
namespace railtools {
#ifdef _WIN32
// MinGW steady_clock can quantize to microseconds. QPC keeps fine-grained
// monotonic timestamps; the log unit remains nanoseconds.
struct BenchClock {
  using duration = std::chrono::nanoseconds;
  using rep = duration::rep;
  using period = duration::period;
  using time_point = std::chrono::time_point<BenchClock>;
  static constexpr bool is_steady = true;
  static time_point now() noexcept {
    static const uint64_t frequency = [] { LARGE_INTEGER value; QueryPerformanceFrequency(&value); return uint64_t(value.QuadPart); }();
    LARGE_INTEGER counter; QueryPerformanceCounter(&counter);
    const uint64_t ticks = uint64_t(counter.QuadPart);
    return time_point(duration((ticks / frequency) * 1000000000ull + (ticks % frequency) * 1000000000ull / frequency));
  }
};
#else
using BenchClock = std::chrono::steady_clock;
#endif
}  // namespace railtools
