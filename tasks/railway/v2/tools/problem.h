#pragma once
#include <algorithm>
#include <array>
#include <functional>
#include <map>
#include <vector>
#include "ticket_system_v2.h"

namespace rail::v2::tools {
using Block = std::array<uint64_t, kSeatsPerTrainDay>;
inline constexpr int first[] = {0, 24, 128}, counts[] = {24, 104, 450};
inline constexpr int widths[] = {3, 4, 5}, rows[] = {8, 13, 18}, cars[] = {1, 2, 4};
inline constexpr const char* letters[] = {"ACF", "ACDF", "ABCDF"};
inline Seat seat_of(int cls, int seat) {
  int n = seat - first[cls], width = widths[cls];
  return {uint8_t(cars[cls] + n / (width * rows[cls])), uint8_t(1 + n / width % rows[cls]), letters[cls][n % width]};
}
inline int seat_index(int cls, Seat seat) {
  if (cls < 0 || cls >= 3 || seat.car < cars[cls] || !seat.row || seat.row > rows[cls]) return -1;
  for (int col = 0; col < widths[cls]; ++col) if (letters[cls][col] == seat.letter) {
    int index = first[cls] + ((seat.car - cars[cls]) * rows[cls] + seat.row - 1) * widths[cls] + col;
    return index < first[cls] + counts[cls] ? index : -1;
  }
  return -1;
}
struct Resolved { uint32_t td, a, b; uint64_t mask; Leg leg; };
struct Order {
  BookRequest request{};
  BookResult result{};
  CellMask active = 0;
  std::array<Resolved, kMaxLegs> legs{};
};
inline bool mask_shape(CellMask mask) { return mask && !(mask & 0x8000); }
inline uint32_t selected_passengers(CellMask mask) {
  uint32_t n = 0;
  for (uint32_t p = 0; p < kMaxPassengers; ++p) n += (mask & CellMask(7u << (p * kMaxLegs))) != 0;
  return n;
}
inline Status selection(const Order& o, CellMask mask) {
  if (mask & ~all_cells(o.request.passenger_count, o.request.leg_count)) return Status::Invalid;
  return (o.active & mask) == mask ? Status::Ok : Status::NotFound;
}
class Problem {
 public:
  struct StopRef { uint32_t train, index; };
  const WorldData* world = nullptr;
  std::vector<std::vector<StopRef>> at;
  explicit Problem(const WorldData& w) : world(&w), at(w.station_count) {
    for (uint32_t t = 0; t < w.train_count; ++t)
      for (uint32_t i = 0; i < w.trains[t].stop_count; ++i) at[stop(t, i).station].push_back({t, i});
  }
  const Stop& stop(uint32_t t, uint32_t i) const { return world->stops[world->trains[t].stop_begin + i]; }
  bool resolve(const LegRef& r, Resolved& out) const {
    if (r.train >= world->train_count || r.day >= kDays) return false;
    int a = -1, b = -1;
    for (uint32_t i = 0; i < world->trains[r.train].stop_count; ++i) {
      if (stop(r.train, i).station == r.from) a = int(i);
      if (stop(r.train, i).station == r.to) b = int(i);
    }
    if (a < 0 || a >= b) return false;
    out = {r.train * kDays + r.day, uint32_t(a), uint32_t(b),
           ((uint64_t(1) << b) - 1) ^ ((uint64_t(1) << a) - 1),
           {r.train, r.day, r.from, r.to, int32_t(r.day) * 1440 + stop(r.train, a).depart,
            int32_t(r.day) * 1440 + stop(r.train, b).arrive}};
    return true;
  }
  bool valid_query(const QueryRequest& q) const {
    return q.from < world->station_count && q.to < world->station_count && q.from != q.to &&
           q.earliest >= 0 && q.earliest < kDays * 1440 && q.k >= 1 && q.k <= kMaxQueryResults &&
           uint32_t(q.seat_class) < 3 && q.passenger_count >= 1 && q.passenger_count <= kMaxPassengers;
  }
  bool book(const BookRequest& b, std::array<Resolved, kMaxLegs>& legs) const {
    if (!b.leg_count || b.leg_count > kMaxLegs || !b.passenger_count || b.passenger_count > kMaxPassengers || uint32_t(b.seat_class) >= 3) return false;
    uint32_t stations[kMaxLegs + 1] = {b.legs[0].from};
    for (uint32_t i = 0; i < b.leg_count; ++i) {
      if (!resolve(b.legs[i], legs[i])) return false;
      stations[i + 1] = b.legs[i].to;
      if (i && (b.legs[i - 1].to != b.legs[i].from || legs[i].leg.depart < legs[i - 1].leg.arrive + 30)) return false;
      for (uint32_t j = 0; j < i; ++j) if (legs[i].td == legs[j].td) return false;
    }
    for (uint32_t i = 0; i <= b.leg_count; ++i)
      for (uint32_t j = 0; j < i; ++j) if (stations[i] == stations[j]) return false;
    return true;
  }
  static bool available(const Block& block, uint64_t mask, SeatClass cls, uint32_t passengers) {
    uint32_t n = 0; int c = int(cls);
    for (int s = first[c]; s < first[c] + counts[c]; ++s)
      if (!(block[s] & mask) && ++n >= passengers) return true;
    return false;
  }
};
using Sequence = std::vector<uint32_t>;
inline Sequence sequence(const Itinerary& it) {
  Sequence result;
  for (uint32_t i = 0; i < it.leg_count; ++i) { result.push_back(it.legs[i].train); result.push_back(it.legs[i].day); }
  return result;
}
inline bool earlier_transfer(const Itinerary& a, const Itinerary& b) {
  for (uint32_t i = 0; i + 1 < a.leg_count; ++i)
    if (a.legs[i].arrive != b.legs[i].arrive) return a.legs[i].arrive < b.legs[i].arrive;
  return false;
}
inline bool ranks_before(const Itinerary& a, const Itinerary& b) {
  if (a.leg_count != b.leg_count) return a.leg_count < b.leg_count;
  int x = a.legs[a.leg_count - 1].arrive, y = b.legs[b.leg_count - 1].arrive;
  if (x != y) return x < y;
  if (a.legs[0].depart != b.legs[0].depart) return a.legs[0].depart > b.legs[0].depart;
  return sequence(a) < sequence(b);
}
class Collector {
  // A train-day sequence fixes first departure and final arrival because a
  // train visits each station once. Canonicalizing transfer stops never
  // changes its rank, so discarding sequences outside top K is safe.
  std::map<Sequence, Itinerary> unique_;
  uint32_t cap_;
 public:
  explicit Collector(uint32_t cap = kMaxQueryResults) : cap_(cap) {}
  void add(const Itinerary& it) {
    auto [pos, inserted] = unique_.emplace(sequence(it), it);
    if (!inserted && earlier_transfer(it, pos->second)) pos->second = it;
    if (unique_.size() > cap_) {
      auto worst = std::max_element(unique_.begin(), unique_.end(), [](const auto& a, const auto& b) { return ranks_before(a.second, b.second); });
      unique_.erase(worst);
    }
  }
  int32_t bound() const {
    if (unique_.size() < cap_) return INT32_MAX;
    const auto& worst = std::max_element(unique_.begin(), unique_.end(), [](const auto& a, const auto& b) { return ranks_before(a.second, b.second); })->second;
    return worst.legs[worst.leg_count - 1].arrive;
  }
  void finish(uint32_t k, QueryResult* out) const {
    std::vector<Itinerary> sorted;
    for (auto& entry : unique_) sorted.push_back(entry.second);
    std::sort(sorted.begin(), sorted.end(), ranks_before);
    *out = {};
    out->count = uint32_t(std::min<size_t>(k, sorted.size()));
    std::copy_n(sorted.begin(), out->count, out->items);
  }
};
using Feasible = std::function<bool(const Resolved&)>;
inline bool extension_valid(const Itinerary& path, const Leg& leg, uint32_t destination) {
  if (path.leg_count && path.legs[0].from == leg.to) return false;
  for (uint32_t i = 0; i < path.leg_count; ++i)
    if ((path.legs[i].train == leg.train && path.legs[i].day == leg.day) || path.legs[i].to == leg.to) return false;
  return leg.to != destination || path.leg_count < kMaxLegs;
}
// Literal baseline traversal. Availability is checked BEFORE canonicalization.
inline QueryResult exhaustive_query(const Problem& problem, const QueryRequest& q, const Feasible& feasible) {
  Collector found(q.k);
  const int32_t end = (q.earliest / 1440 + 1) * 1440;
  std::vector<std::vector<Problem::StopRef>> ending(problem.world->station_count);
  for (auto target : problem.at[q.to])
    for (uint32_t i = 0; i < target.index; ++i) ending[problem.stop(target.train, i).station].push_back({target.train, i});
  uint32_t length = 1;
  std::function<void(Itinerary, uint32_t, int32_t)> visit = [&](Itinerary path, uint32_t from, int32_t ready) {
    if (ready >= found.bound()) return;
    bool last = path.leg_count + 1 == length;
    for (auto s : last ? ending[from] : problem.at[from]) for (uint32_t d = 0; d < kDays; ++d) {
      int32_t depart = int32_t(d) * 1440 + problem.stop(s.train, s.index).depart;
      if (depart < ready || depart >= found.bound() || (!path.leg_count && depart >= end)) continue;
      for (uint32_t j = s.index + 1; j < problem.world->trains[s.train].stop_count; ++j) {
        if ((problem.stop(s.train, j).station == q.to) != last) continue;
        Resolved r;
        problem.resolve({s.train, d, from, problem.stop(s.train, j).station}, r);
        if (r.leg.arrive > found.bound() || !extension_valid(path, r.leg, q.to) || !feasible(r)) continue;
        auto next = path; next.legs[next.leg_count++] = r.leg;
        if (r.leg.to == q.to) found.add(next);
        else if (next.leg_count < length) visit(next, r.leg.to, r.leg.arrive + 30);
      }
    }
  };
  QueryResult out{};
  for (length = 1; length <= kMaxLegs && out.count < q.k; ++length) {
    visit({}, q.from, q.earliest); found.finish(q.k, &out);
  }
  return out;
}
}  // namespace rail::v2::tools
