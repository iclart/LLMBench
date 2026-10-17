// Deterministic random number generation for the railway tools.
//
// Everything here uses integer arithmetic only, so a given seed produces
// byte-identical output on every platform and standard library. Do not use
// <random> distributions in generator code: their output is
// implementation-defined.
#pragma once

#include <cstdint>
#include <stdexcept>
#include <utility>
#include <vector>

namespace railtools {

inline uint64_t splitmix64(uint64_t& state) {
  uint64_t z = (state += 0x9E3779B97F4A7C15ull);
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
  z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
  return z ^ (z >> 31);
}

// xoshiro256** by Blackman and Vigna.
class Rng {
 public:
  explicit Rng(uint64_t seed) {
    uint64_t sm = seed;
    for (auto& word : s_) word = splitmix64(sm);
  }

  uint64_t next() {
    const uint64_t result = rotl(s_[1] * 5, 7) * 9;
    const uint64_t t = s_[1] << 17;
    s_[2] ^= s_[0];
    s_[3] ^= s_[1];
    s_[1] ^= s_[2];
    s_[0] ^= s_[3];
    s_[2] ^= t;
    s_[3] = rotl(s_[3], 45);
    return result;
  }

  // Uniform integer in [0, n). Unbiased (rejection sampling).
  uint64_t below(uint64_t n) {
    if (n == 0) throw std::invalid_argument("Rng::below(0)");
    const uint64_t limit = UINT64_MAX - UINT64_MAX % n;
    uint64_t x;
    do {
      x = next();
    } while (x >= limit);
    return x % n;
  }

  // Uniform integer in [lo, hi].
  int64_t range(int64_t lo, int64_t hi) {
    if (lo > hi) throw std::invalid_argument("Rng::range: lo > hi");
    return lo + static_cast<int64_t>(below(static_cast<uint64_t>(hi - lo) + 1));
  }

  // True with probability permille / 1000.
  bool chance(int64_t permille) { return static_cast<int64_t>(below(1000)) < permille; }

  // Independent stream derived from this one, e.g. one per output section.
  Rng fork() { return Rng(next()); }

  template <class T>
  void shuffle(std::vector<T>& v) {
    for (size_t i = v.size(); i > 1; --i) {
      std::swap(v[i - 1], v[static_cast<size_t>(below(i))]);
    }
  }

 private:
  static uint64_t rotl(uint64_t x, int k) { return (x << k) | (x >> (64 - k)); }
  uint64_t s_[4];
};

// Picks index i with probability weights[i] / sum(weights).
class WeightedPicker {
 public:
  WeightedPicker() = default;
  explicit WeightedPicker(const std::vector<uint64_t>& weights) {
    cumulative_.reserve(weights.size());
    uint64_t total = 0;
    for (uint64_t w : weights) {
      total += w;
      cumulative_.push_back(total);
    }
    if (total == 0) throw std::invalid_argument("WeightedPicker: all weights are zero");
  }

  size_t pick(Rng& rng) const {
    const uint64_t x = rng.below(cumulative_.back());
    size_t lo = 0, hi = cumulative_.size() - 1;
    while (lo < hi) {
      const size_t mid = (lo + hi) / 2;
      if (cumulative_[mid] > x) hi = mid; else lo = mid + 1;
    }
    return lo;
  }

  size_t size() const { return cumulative_.size(); }

 private:
  std::vector<uint64_t> cumulative_;
};

}  // namespace railtools
