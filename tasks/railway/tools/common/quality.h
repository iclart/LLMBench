// Booking quality (TASK.md 6): how many purchases succeed, how much travel
// they carry, and how much of the capacity of contested train-days is sold.
// Shared by railjudge and railrun.
//
// Satisfaction      = purchases that got a ticket / purchase intents whose
// query found at least one itinerary (the others could never be served).
// Passenger-minutes = the transport delivered (railways count passenger-km;
// the world has no distances): passengers x riding minutes of every
// successful purchase. Each purchase counts the riding minutes of its FIRST
// itinerary, whichever itinerary was booked, so refusing fast trains to push
// passengers onto slow ones gains nothing.
// Utilization       = seat-minutes sold / seat-minutes on sale, over the
// (train-day, class) pairs where some book was turned down for lack of seats
// on that train-day (SoldOut, counting only the legs that were short) or was
// Withheld (every leg). Seat-minutes of a ticket run from the arrival at its
// boarding stop to the arrival at its alighting stop; sold seats are those of
// main-phase orders still live when the main phase ends. Shown, not scored.
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

// Riding minutes of an itinerary, from boarding to alighting on every leg.
inline int64_t ride_minutes(const rail::Itinerary& it) {
  int64_t m = 0;
  for (uint32_t j = 0; j < it.leg_count && j < uint32_t(rail::kMaxLegs); ++j) m += it.legs[j].arrive - it.legs[j].depart;
  return m;
}

struct Quality {
  uint64_t purchases = 0, purchases_ok = 0, sold_out = 0, withheld = 0;
  std::set<std::pair<uint32_t, int>> unmet;  // (train-day, class) turned someone down
  int64_t demand_minutes = 0, passenger_minutes = 0;  // passenger-minutes asked for / ticketed
  int64_t sold_minutes = 0, capacity_minutes = 0;

  // One purchase whose query found `first`, its first itinerary.
  void add_purchase(uint32_t passengers, const rail::Itinerary& first, bool ok) {
    const int64_t m = int64_t(passengers) * ride_minutes(first);
    ++purchases;
    purchases_ok += ok;
    demand_minutes += m;
    passenger_minutes += ok ? m : 0;
  }

  double satisfaction() const { return purchases ? double(purchases_ok) / double(purchases) : 1.0; }
  // Share of the passenger-minutes asked for that were carried.
  double transport_share() const { return demand_minutes ? double(passenger_minutes) / double(demand_minutes) : 1.0; }
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
