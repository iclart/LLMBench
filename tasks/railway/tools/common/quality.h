// Booking quality (TASK.md 6): how many purchases succeed, and how much of the
// capacity of contested train-days is sold. Shared by railjudge and railrun.
//
// Satisfaction = purchases that got a ticket / purchase intents.
// Utilization  = seat-minutes sold / seat-minutes on sale, over the
// (train-day, class) pairs where some book was turned down for lack of seats
// on that train-day (SoldOut, counting only the legs that were short) or was
// Withheld (every leg). Seat-minutes of a ticket run from the arrival at its
// boarding stop to the arrival at its alighting stop; sold seats are those of
// main-phase orders still live when the main phase ends.
#pragma once

#include <cstdint>
#include <set>
#include <utility>
#include <vector>

#include "seats.h"
#include "world.h"

namespace railtools {

// Minutes covered by the stop-interval mask `mask` (bit i = segment from stop
// i to stop i+1).
inline int64_t mask_minutes(const WorldTrain& t, uint64_t mask) {
  int64_t m = 0;
  for (size_t i = 0; i + 1 < t.stops.size(); ++i)
    if (mask >> i & 1) m += t.stops[i + 1].arrive - t.stops[i].arrive;
  return m;
}

inline int64_t train_minutes(const WorldTrain& t) { return t.stops.back().arrive - t.stops.front().arrive; }

struct Quality {
  uint64_t purchases = 0, purchases_ok = 0, sold_out = 0, withheld = 0;
  std::set<std::pair<uint32_t, int>> unmet;  // (train-day, class) turned someone down
  int64_t sold_minutes = 0, capacity_minutes = 0;

  double satisfaction() const { return purchases ? double(purchases_ok) / double(purchases) : 1.0; }
  // Negative when no train-day turned anyone down.
  double utilization() const { return capacity_minutes ? double(sold_minutes) / double(capacity_minutes) : -1.0; }

  // `sold(td, seat)` returns the stop-interval mask held on that seat at the end of the main phase.
  template <typename Sold>
  void measure(const World& w, Sold sold) {
    sold_minutes = capacity_minutes = 0;
    for (const auto& [td, cls] : unmet) {
      const WorldTrain& t = w.trains[td / rail::kDays];
      const ClassInfo& ci = class_info(static_cast<rail::SeatClass>(cls));
      capacity_minutes += int64_t(ci.seats()) * train_minutes(t);
      for (int s = ci.first_seat; s < ci.first_seat + ci.seats(); ++s) sold_minutes += mask_minutes(t, sold(td, s));
    }
  }
};

}  // namespace railtools
