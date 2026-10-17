// Baseline reference engine: the 0-point anchor of the performance score.
//
// What a careful but unambitious engineer would write first:
// - one global mutex taken by every call, query() included;
// - seats kept as plain lists of sold [from, to) stop ranges, first fit;
// - query() searches level by level (direct, one transfer, two transfers),
//   keeping only the best k itineraries and abandoning partial routes that
//   can no longer beat them. Per query it notes which trains reach the
//   destination and which stations can still get there with the legs left;
//   there is no precomputed connection index.
#include <algorithm>
#include <cstdint>
#include <mutex>
#include <unordered_map>
#include <utility>
#include <vector>

#include "ticket_system.h"

namespace rail {

namespace {

struct ClassLayout {
  int first_car, rows_per_car, first_seat, seat_count;
  const char* letters;
  int width;
};

constexpr ClassLayout kLayouts[kSeatClassCount] = {
    {1, 8, 0, 24, "ACF", 3},
    {2, 13, 24, 104, "ACDF", 4},
    {4, 18, 128, 450, "ABCDF", 5},
};

struct Route {
  uint32_t n = 0;
  Leg legs[kMaxLegs];
};

// TASK.md 4.4 order for routes with the same number of legs.
bool better(const Route& a, const Route& b) {
  const int32_t aa = a.legs[a.n - 1].arrive, ba = b.legs[b.n - 1].arrive;
  if (aa != ba) return aa < ba;
  if (a.legs[0].depart != b.legs[0].depart) return a.legs[0].depart > b.legs[0].depart;
  for (uint32_t i = 0; i < a.n; ++i) {
    if (a.legs[i].train != b.legs[i].train) return a.legs[i].train < b.legs[i].train;
    if (a.legs[i].day != b.legs[i].day) return a.legs[i].day < b.legs[i].day;
  }
  return false;
}

bool same_train_days(const Route& a, const Route& b) {
  for (uint32_t i = 0; i < a.n; ++i)
    if (a.legs[i].train != b.legs[i].train || a.legs[i].day != b.legs[i].day) return false;
  return true;
}

// The best `cap` routes seen so far, sorted, one per train-day sequence.
struct Best {
  size_t cap = 0;
  std::vector<Route> routes;

  // Routes arriving later than this cannot get in.
  int32_t limit() const {
    if (routes.size() < cap) return INT32_MAX;
    const Route& worst = routes.back();
    return worst.legs[worst.n - 1].arrive;
  }

  void add(const Route& r) {
    for (Route& have : routes) {
      if (!same_train_days(have, r)) continue;
      for (uint32_t t = 0; t + 1 < r.n; ++t) {  // keep the earliest transfers
        if (r.legs[t].arrive != have.legs[t].arrive) {
          if (r.legs[t].arrive < have.legs[t].arrive) have = r;
          return;
        }
      }
      return;
    }
    if (routes.size() == cap && !better(r, routes.back())) return;
    routes.insert(std::upper_bound(routes.begin(), routes.end(), r, better), r);
    if (routes.size() > cap) routes.pop_back();
  }
};

class BaselineSystem final : public TicketSystem {
 public:
  void init(const WorldData& world) override {
    std::lock_guard<std::mutex> lock(mu_);
    world_ = &world;
    trains_at_.resize(world.station_count);
    for (uint32_t t = 0; t < world.train_count; ++t) {
      for (uint32_t i = 0; i < world.trains[t].stop_count; ++i) trains_at_[stop(t, i).station].push_back({t, i});
    }
    sold_.resize(size_t(world.train_count) * kDays);
    dest_index_.assign(world.train_count, -1);
    hops_.assign(world.station_count, 0);
  }

  Status query(const QueryRequest& req, QueryResult* out) override {
    std::lock_guard<std::mutex> lock(mu_);
    if (req.from >= world_->station_count || req.to >= world_->station_count || req.from == req.to) {
      return Status::Invalid;
    }
    if (req.earliest < 0 || req.earliest >= kDays * kMinutesPerDay) return Status::Invalid;
    if (req.k < 1 || req.k > static_cast<uint32_t>(kMaxQueryResults)) return Status::Invalid;

    // Trains that stop at the destination, the stations they pass before it
    // (one leg away), and the stations one transfer away from those.
    clear_marks();
    for (const StopRef& r : trains_at_[req.to]) {
      mark_train(r.train, int(r.index));
      for (uint32_t i = 0; i < r.index; ++i) mark_station(stop(r.train, i).station, 1);
    }
    bool two_hops_known = false;

    out->count = 0;
    for (uint32_t legs = 1; legs <= static_cast<uint32_t>(kMaxLegs) && out->count < req.k; ++legs) {
      if (legs == 3 && !two_hops_known) {
        mark_two_hops();
        two_hops_known = true;
      }
      Best best;
      best.cap = req.k - out->count;
      Search s{req, legs, (req.earliest / kMinutesPerDay + 1) * kMinutesPerDay, best, {}};
      for (const StopRef& r : trains_at_[req.from]) {
        for (uint32_t day = 0; day < static_cast<uint32_t>(kDays); ++day) {
          const int32_t dep = int32_t(day) * kMinutesPerDay + stop(r.train, r.index).depart;
          if (dep >= req.earliest && dep < s.window_end) ride(s, 0, r.train, day, r.index);
        }
      }
      for (const Route& route : best.routes) {
        Itinerary& it = out->items[out->count++];
        it.leg_count = route.n;
        std::copy(route.legs, route.legs + route.n, it.legs);
      }
    }
    return Status::Ok;
  }

  Status book(const BookRequest& req, BookResult* out) override {
    std::lock_guard<std::mutex> lock(mu_);
    const uint32_t n_legs = req.leg_count;
    if (n_legs < 1 || n_legs > static_cast<uint32_t>(kMaxLegs)) return Status::Invalid;
    if (static_cast<uint8_t>(req.seat_class) >= kSeatClassCount) return Status::Invalid;
    if (req.passenger_count < 1 || req.passenger_count > static_cast<uint32_t>(kMaxPassengers)) return Status::Invalid;

    uint32_t a[kMaxLegs], b[kMaxLegs];
    int32_t dep[kMaxLegs], arr[kMaxLegs];
    for (uint32_t i = 0; i < n_legs; ++i) {
      const LegRef& l = req.legs[i];
      if (l.train >= world_->train_count || l.day >= static_cast<uint32_t>(kDays)) return Status::Invalid;
      if (!find_stop(l.train, l.from, &a[i]) || !find_stop(l.train, l.to, &b[i]) || a[i] >= b[i]) {
        return Status::Invalid;
      }
      dep[i] = int32_t(l.day) * kMinutesPerDay + stop(l.train, a[i]).depart;
      arr[i] = int32_t(l.day) * kMinutesPerDay + stop(l.train, b[i]).arrive;
    }
    uint32_t stations[kMaxLegs + 1] = {req.legs[0].from};
    for (uint32_t i = 0; i < n_legs; ++i) {
      if (i + 1 < n_legs) {
        if (req.legs[i].to != req.legs[i + 1].from) return Status::Invalid;
        if (dep[i + 1] < arr[i] + kMinTransferMinutes) return Status::Invalid;
      }
      for (uint32_t j = 0; j < i; ++j) {
        if (req.legs[i].train == req.legs[j].train && req.legs[i].day == req.legs[j].day) return Status::Invalid;
      }
      if (std::find(stations, stations + i + 1, req.legs[i].to) != stations + i + 1) return Status::Invalid;
      stations[i + 1] = req.legs[i].to;
    }

    const ClassLayout& c = kLayouts[static_cast<int>(req.seat_class)];
    int picked[kMaxLegs][kMaxPassengers];
    for (uint32_t i = 0; i < n_legs; ++i) {
      auto& seats = seats_of(req.legs[i].train, req.legs[i].day);
      uint32_t found = 0;
      for (int k = 0; k < c.seat_count && found < req.passenger_count; ++k) {
        if (is_free(seats[c.first_seat + k], a[i], b[i])) picked[i][found++] = k;
      }
      if (found < req.passenger_count) return Status::SoldOut;
    }

    const uint64_t id = next_id_++;
    std::vector<Hold>& holds = orders_[id];
    for (uint32_t i = 0; i < n_legs; ++i) {
      const uint32_t td = req.legs[i].train * kDays + req.legs[i].day;
      for (uint32_t p = 0; p < req.passenger_count; ++p) {
        const int k = picked[i][p];
        sold_[td][c.first_seat + k].push_back({a[i], b[i]});
        holds.push_back({td, c.first_seat + k, a[i], b[i]});
        const int row = k / c.width;
        out->seats[i][p] = {uint8_t(c.first_car + row / c.rows_per_car), uint8_t(1 + row % c.rows_per_car),
                            c.letters[k % c.width]};
      }
    }
    out->order_id = id;
    return Status::Ok;
  }

  Status refund(uint64_t order_id) override {
    std::lock_guard<std::mutex> lock(mu_);
    const auto it = orders_.find(order_id);
    if (it == orders_.end()) return Status::NotFound;
    for (const Hold& h : it->second) {
      auto& ranges = sold_[h.td][h.seat];
      ranges.erase(std::find(ranges.begin(), ranges.end(), std::make_pair(h.from, h.to)));
    }
    orders_.erase(it);
    return Status::Ok;
  }

 private:
  struct StopRef {
    uint32_t train, index;
  };
  struct Hold {
    uint32_t td;
    int seat;
    uint32_t from, to;
  };
  struct Search {
    const QueryRequest& req;
    uint32_t legs;
    int32_t window_end;
    Best& best;
    Route route;
  };
  using Ranges = std::vector<std::pair<uint32_t, uint32_t>>;

  const Stop& stop(uint32_t train, uint32_t index) const {
    return world_->stops[world_->trains[train].stop_begin + index];
  }

  bool find_stop(uint32_t train, uint32_t station, uint32_t* index) const {
    for (uint32_t i = 0; i < world_->trains[train].stop_count; ++i) {
      if (stop(train, i).station == station) {
        *index = i;
        return true;
      }
    }
    return false;
  }

  // Rides train-day (train, day) from stop `board` as leg `depth` of s.route.
  void ride(Search& s, uint32_t depth, uint32_t train, uint32_t day, uint32_t board) const {
    if (depth >= static_cast<uint32_t>(kMaxLegs)) return;
    for (uint32_t d = 0; d < depth; ++d) {
      if (s.route.legs[d].train == train && s.route.legs[d].day == day) return;
    }
    const bool last = depth + 1 == s.legs;
    if (last && dest_index_[train] <= int(board)) return;
    const int32_t base = int32_t(day) * kMinutesPerDay;
    for (uint32_t i = board + 1; i < world_->trains[train].stop_count; ++i) {
      const int32_t arrive = base + stop(train, i).arrive;
      // Arrival only grows along the train and with every further leg.
      if (last ? arrive > s.best.limit() : arrive + kMinTransferMinutes >= s.best.limit()) break;
      const uint32_t at = stop(train, i).station;
      s.route.legs[depth] = {train, day, stop(train, board).station, at, base + stop(train, board).depart, arrive};
      s.route.n = depth + 1;
      if (last) {
        if (at == s.req.to) s.best.add(s.route);
        continue;
      }
      if (at == s.req.to || at == s.req.from) continue;
      // Legs left after this one: the next stop must be that many hops away.
      if (hops_[at] == 0 || hops_[at] > s.legs - depth - 1) continue;
      bool seen = false;
      for (uint32_t d = 0; d < depth; ++d) seen |= s.route.legs[d].to == at;
      if (seen) continue;
      for (const StopRef& r : trains_at_[at]) {
        for (uint32_t nd = 0; nd < static_cast<uint32_t>(kDays); ++nd) {
          if (int32_t(nd) * kMinutesPerDay + stop(r.train, r.index).depart >= arrive + kMinTransferMinutes) {
            ride(s, depth + 1, r.train, nd, r.index);
          }
        }
      }
    }
  }

  // Per-query marks, reset by undoing only what was set.
  void mark_train(uint32_t train, int index) {
    if (dest_index_[train] < 0) set_trains_.push_back(train);
    dest_index_[train] = index;
  }
  void mark_station(uint32_t station, uint8_t hops) {
    if (hops_[station] == 0) set_stations_.push_back(station);
    hops_[station] = hops;
  }
  void clear_marks() {
    for (uint32_t t : set_trains_) dest_index_[t] = -1;
    for (uint32_t st : set_stations_) hops_[st] = 0;
    set_trains_.clear();
    set_stations_.clear();
  }

  // hops_ 2: stations from which some train reaches a station with hops_ 1.
  void mark_two_hops() {
    const size_t one_hop = set_stations_.size();
    for (size_t k = 0; k < one_hop; ++k) {
      for (const StopRef& r : trains_at_[set_stations_[k]]) {
        for (uint32_t i = 0; i < r.index; ++i) {
          const uint32_t st = stop(r.train, i).station;
          if (hops_[st] == 0) mark_station(st, 2);
        }
      }
    }
  }

  std::vector<Ranges>& seats_of(uint32_t train, uint32_t day) {
    auto& seats = sold_[train * kDays + day];
    if (seats.empty()) seats.resize(kSeatsPerTrainDay);
    return seats;
  }

  static bool is_free(const Ranges& sold, uint32_t from, uint32_t to) {
    for (const auto& [a, b] : sold) {
      if (a < to && from < b) return false;
    }
    return true;
  }

  std::mutex mu_;
  const WorldData* world_ = nullptr;
  std::vector<std::vector<StopRef>> trains_at_;
  std::vector<int> dest_index_;          // per train: its stop index at the query's destination, or -1
  std::vector<uint8_t> hops_;            // per station: legs needed to the destination (1, 2), 0 unknown
  std::vector<uint32_t> set_trains_, set_stations_;  // entries of the above set by this query
  std::vector<std::vector<Ranges>> sold_;  // [train-day][seat]
  std::unordered_map<uint64_t, std::vector<Hold>> orders_;
  uint64_t next_id_ = 1;
};

}  // namespace

TicketSystem* create_ticket_system() { return new BaselineSystem(); }

}  // namespace rail
