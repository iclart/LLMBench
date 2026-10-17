// Baseline reference engine: the 0-point anchor of the performance score.
//
// Deliberately naive and literal to TASK.md: one global mutex around every
// booking operation, seat occupancy kept as plain interval lists, and query()
// enumerates every candidate itinerary, deduplicates with a std::map and
// sorts. It is also the oracle the strong reference's query() is checked
// against, so clarity wins over speed everywhere.
#include <algorithm>
#include <array>
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <unordered_map>
#include <utility>
#include <vector>

#include "ticket_system.h"

namespace rail {

namespace {

// ---------------------------------------------------------------- seats

struct ClassLayout {
  int first_car, car_count, rows;
  const char* letters;
  int first_seat;  // index of the class's first seat within a train-day
};

constexpr ClassLayout kLayouts[kSeatClassCount] = {
    {1, 1, 8, "ACF", 0},
    {2, 2, 13, "ACDF", 24},
    {4, 5, 18, "ABCDF", 128},
};

int letters_per_row(const ClassLayout& c) {
  int n = 0;
  while (c.letters[n]) ++n;
  return n;
}

int seat_count(const ClassLayout& c) { return c.car_count * c.rows * letters_per_row(c); }

Seat seat_of(const ClassLayout& c, int k) {  // k-th seat of the class
  const int per_row = letters_per_row(c);
  const int per_car = c.rows * per_row;
  Seat s;
  s.car = static_cast<uint8_t>(c.first_car + k / per_car);
  s.row = static_cast<uint8_t>(1 + (k % per_car) / per_row);
  s.letter = c.letters[k % per_row];
  return s;
}

// ---------------------------------------------------------------- itineraries

struct FullLeg {
  uint32_t train, day, from, to;
  int32_t depart, arrive;
};

struct Candidate {
  uint32_t n = 0;
  FullLeg legs[kMaxLegs];
};

using Sequence = std::vector<std::pair<uint32_t, uint32_t>>;  // (train, day) per leg

Sequence sequence_of(const Candidate& c) {
  Sequence s;
  for (uint32_t i = 0; i < c.n; ++i) s.push_back({c.legs[i].train, c.legs[i].day});
  return s;
}

std::vector<int32_t> transfer_arrivals(const Candidate& c) {
  std::vector<int32_t> v;
  for (uint32_t i = 0; i + 1 < c.n; ++i) v.push_back(c.legs[i].arrive);
  return v;
}

// TASK.md 4.4: fewer legs, earlier arrival, later departure, smaller sequence.
bool ranks_before(const Candidate& a, const Candidate& b) {
  if (a.n != b.n) return a.n < b.n;
  const int32_t aa = a.legs[a.n - 1].arrive, ba = b.legs[b.n - 1].arrive;
  if (aa != ba) return aa < ba;
  if (a.legs[0].depart != b.legs[0].depart) return a.legs[0].depart > b.legs[0].depart;
  return sequence_of(a) < sequence_of(b);
}

class BaselineSystem final : public TicketSystem {
 public:
  void init(const WorldData& world) override {
    world_ = &world;
    trains_at_.resize(world.station_count);
    for (uint32_t t = 0; t < world.train_count; ++t) {
      const Train& tr = world.trains[t];
      for (uint32_t i = 0; i < tr.stop_count; ++i) trains_at_[stop(t, i).station].push_back({t, i});
    }
    seats_.resize(size_t(world.train_count) * kDays);
  }

  Status query(const QueryRequest& req, QueryResult* out) override {
    const WorldData& w = *world_;
    if (req.from >= w.station_count || req.to >= w.station_count || req.from == req.to) return Status::Invalid;
    if (req.earliest < 0 || req.earliest >= kDays * kMinutesPerDay) return Status::Invalid;
    if (req.k < 1 || req.k > static_cast<uint32_t>(kMaxQueryResults)) return Status::Invalid;

    std::vector<Candidate> results;
    for (uint32_t legs = 1; legs <= static_cast<uint32_t>(kMaxLegs) && results.size() < req.k; ++legs) {
      std::map<Sequence, Candidate> found;
      search(req, legs, found);
      std::vector<Candidate> level;
      for (auto& [seq, cand] : found) level.push_back(cand);
      std::sort(level.begin(), level.end(), ranks_before);
      for (const auto& c : level) {
        if (results.size() == req.k) break;
        results.push_back(c);
      }
    }

    out->count = static_cast<uint32_t>(results.size());
    for (size_t i = 0; i < results.size(); ++i) {
      Itinerary& it = out->items[i];
      it.leg_count = results[i].n;
      for (uint32_t j = 0; j < results[i].n; ++j) {
        const FullLeg& l = results[i].legs[j];
        it.legs[j] = {l.train, l.day, l.from, l.to, l.depart, l.arrive};
      }
    }
    return Status::Ok;
  }

  Status book(const BookRequest& req, BookResult* out) override {
    if (req.leg_count < 1 || req.leg_count > static_cast<uint32_t>(kMaxLegs)) return Status::Invalid;
    if (static_cast<uint8_t>(req.seat_class) >= kSeatClassCount) return Status::Invalid;
    if (req.passenger_count < 1 || req.passenger_count > static_cast<uint32_t>(kMaxPassengers)) return Status::Invalid;
    FullLeg legs[kMaxLegs];
    uint32_t from_index[kMaxLegs], to_index[kMaxLegs];
    for (uint32_t i = 0; i < req.leg_count; ++i) {
      const LegRef& r = req.legs[i];
      if (!resolve(r, &legs[i], &from_index[i], &to_index[i])) return Status::Invalid;
    }
    if (!connects(legs, req.leg_count)) return Status::Invalid;

    const ClassLayout& layout = kLayouts[static_cast<int>(req.seat_class)];
    const int count = seat_count(layout);

    std::lock_guard<std::mutex> lock(mu_);
    // Pick the first free seats of every leg before changing anything.
    std::vector<std::vector<int>> picked(req.leg_count);
    for (uint32_t i = 0; i < req.leg_count; ++i) {
      auto& seats = seats_of(legs[i].train, legs[i].day);
      for (int k = 0; k < count && picked[i].size() < req.passenger_count; ++k) {
        const int seat = layout.first_seat + k;
        if (is_free(seats[seat], from_index[i], to_index[i])) picked[i].push_back(seat);
      }
      if (picked[i].size() < req.passenger_count) return Status::SoldOut;
    }

    Order order;
    for (uint32_t i = 0; i < req.leg_count; ++i) {
      auto& seats = seats_of(legs[i].train, legs[i].day);
      for (uint32_t p = 0; p < req.passenger_count; ++p) {
        const int seat = picked[i][p];
        seats[seat].push_back({from_index[i], to_index[i]});
        order.holds.push_back({legs[i].train, legs[i].day, seat, from_index[i], to_index[i]});
        out->seats[i][p] = seat_of(layout, seat - layout.first_seat);
      }
    }
    out->order_id = next_order_id_++;
    orders_.emplace(out->order_id, std::move(order));
    return Status::Ok;
  }

  Status refund(uint64_t order_id) override {
    std::lock_guard<std::mutex> lock(mu_);
    const auto it = orders_.find(order_id);
    if (it == orders_.end()) return Status::NotFound;
    for (const Hold& h : it->second.holds) {
      auto& intervals = seats_of(h.train, h.day)[h.seat];
      intervals.erase(std::find(intervals.begin(), intervals.end(), std::make_pair(h.from_index, h.to_index)));
    }
    orders_.erase(it);
    return Status::Ok;
  }

 private:
  struct StopRef {
    uint32_t train, index;
  };
  struct Hold {
    uint32_t train, day;
    int seat;
    uint32_t from_index, to_index;
  };
  struct Order {
    std::vector<Hold> holds;
  };
  using Intervals = std::vector<std::pair<uint32_t, uint32_t>>;  // sold [from, to) stop ranges

  const Stop& stop(uint32_t train, uint32_t index) const {
    return world_->stops[world_->trains[train].stop_begin + index];
  }

  int32_t abs_depart(uint32_t train, uint32_t day, uint32_t index) const {
    return int32_t(day) * kMinutesPerDay + stop(train, index).depart;
  }
  int32_t abs_arrive(uint32_t train, uint32_t day, uint32_t index) const {
    return int32_t(day) * kMinutesPerDay + stop(train, index).arrive;
  }

  bool resolve(const LegRef& r, FullLeg* leg, uint32_t* from_index, uint32_t* to_index) const {
    const WorldData& w = *world_;
    if (r.train >= w.train_count || r.day >= static_cast<uint32_t>(kDays)) return false;
    bool has_from = false, has_to = false;
    for (uint32_t i = 0; i < w.trains[r.train].stop_count; ++i) {
      if (stop(r.train, i).station == r.from) has_from = true, *from_index = i;
      if (stop(r.train, i).station == r.to) has_to = true, *to_index = i;
    }
    if (!has_from || !has_to || *from_index >= *to_index) return false;
    *leg = {r.train, r.day, r.from, r.to, abs_depart(r.train, r.day, *from_index),
            abs_arrive(r.train, r.day, *to_index)};
    return true;
  }

  // TASK.md 4.1 rules 2-4.
  static bool connects(const FullLeg* legs, uint32_t n) {
    for (uint32_t i = 0; i + 1 < n; ++i) {
      if (legs[i].to != legs[i + 1].from) return false;
      if (legs[i + 1].depart < legs[i].arrive + kMinTransferMinutes) return false;
    }
    std::vector<uint32_t> stations{legs[0].from};
    for (uint32_t i = 0; i < n; ++i) stations.push_back(legs[i].to);
    for (size_t i = 0; i < stations.size(); ++i)
      for (size_t j = i + 1; j < stations.size(); ++j)
        if (stations[i] == stations[j]) return false;
    for (uint32_t i = 0; i < n; ++i)
      for (uint32_t j = i + 1; j < n; ++j)
        if (legs[i].train == legs[j].train && legs[i].day == legs[j].day) return false;
    return true;
  }

  struct SearchContext {
    const QueryRequest& req;
    uint32_t legs;
    // into_dest[s]: (train, stop index of s) for trains that later stop at req.to.
    const std::vector<std::vector<StopRef>>& into_dest;
    std::map<Sequence, Candidate>& found;
  };

  // Every itinerary with exactly `legs` legs that answers `req`, keyed by its
  // train-day sequence, keeping the one with the earliest transfers.
  void search(const QueryRequest& req, uint32_t legs, std::map<Sequence, Candidate>& found) const {
    std::vector<std::vector<StopRef>> into_dest(world_->station_count);
    for (const StopRef& s : trains_at_[req.to]) {
      for (uint32_t i = 0; i < s.index; ++i) into_dest[stop(s.train, i).station].push_back({s.train, i});
    }
    const SearchContext ctx{req, legs, into_dest, found};
    const int32_t window_end = (req.earliest / kMinutesPerDay + 1) * kMinutesPerDay;
    Candidate partial;
    for (const StopRef& s : legs == 1 ? into_dest[req.from] : trains_at_[req.from]) {
      for (uint32_t day = 0; day < static_cast<uint32_t>(kDays); ++day) {
        const int32_t dep = abs_depart(s.train, day, s.index);
        if (dep < req.earliest || dep >= window_end) continue;
        extend(ctx, partial, 0, s.train, day, s.index);
      }
    }
  }

  // Rides train-day (train, day) from stop `board` as leg number `depth`.
  void extend(const SearchContext& ctx, Candidate& partial, uint32_t depth, uint32_t train, uint32_t day,
              uint32_t board) const {
    if (depth >= static_cast<uint32_t>(kMaxLegs)) return;
    for (uint32_t d = 0; d < depth; ++d) {
      if (partial.legs[d].train == train && partial.legs[d].day == day) return;
    }
    const uint32_t from = stop(train, board).station;
    for (uint32_t i = board + 1; i < world_->trains[train].stop_count; ++i) {
      const uint32_t at = stop(train, i).station;
      partial.legs[depth] = {train, day, from, at, abs_depart(train, day, board), abs_arrive(train, day, i)};
      partial.n = depth + 1;
      if (depth + 1 == ctx.legs) {
        if (at == ctx.req.to) record(partial, ctx.found);
        continue;
      }
      // Transfer at `at`: it must differ from the origin, destination and earlier transfers.
      if (at == ctx.req.to || at == ctx.req.from) continue;
      bool repeated = false;
      for (uint32_t d = 0; d < depth; ++d) repeated |= partial.legs[d].to == at;
      if (repeated) continue;
      const bool next_is_last = depth + 2 == ctx.legs;
      const int32_t ready = partial.legs[depth].arrive + kMinTransferMinutes;
      for (const StopRef& s : next_is_last ? ctx.into_dest[at] : trains_at_[at]) {
        for (uint32_t nd = 0; nd < static_cast<uint32_t>(kDays); ++nd) {
          if (abs_depart(s.train, nd, s.index) < ready) continue;
          extend(ctx, partial, depth + 1, s.train, nd, s.index);
        }
      }
    }
  }

  static void record(const Candidate& c, std::map<Sequence, Candidate>& found) {
    const auto [it, inserted] = found.emplace(sequence_of(c), c);
    if (!inserted && transfer_arrivals(c) < transfer_arrivals(it->second)) it->second = c;
  }

  std::vector<Intervals>& seats_of(uint32_t train, uint32_t day) {
    auto& seats = seats_[size_t(train) * kDays + day];
    if (seats.empty()) seats.resize(kSeatsPerTrainDay);
    return seats;
  }

  static bool is_free(const Intervals& sold, uint32_t from, uint32_t to) {
    for (const auto& [a, b] : sold) {
      if (a < to && from < b) return false;
    }
    return true;
  }

  const WorldData* world_ = nullptr;
  std::vector<std::vector<StopRef>> trains_at_;

  std::mutex mu_;
  std::vector<std::vector<Intervals>> seats_;  // [train * kDays + day][seat]
  std::unordered_map<uint64_t, Order> orders_;
  uint64_t next_order_id_ = 1;
};

}  // namespace

TicketSystem* create_ticket_system() { return new BaselineSystem(); }

}  // namespace rail
