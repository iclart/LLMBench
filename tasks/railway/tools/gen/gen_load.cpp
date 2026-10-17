#include <algorithm>
#include <numeric>
#include <stdexcept>
#include <string>
#include <vector>

#include "generate.h"
#include "rng.h"

namespace railtools {

namespace {

const char* const kScenarioNames[] = {"daily", "spring", "refund_wave"};

struct IntentWeights {
  int64_t purchase, browse, refund, dup_refund, bad;

  IntentWeights(Config& c, const std::string& prefix) {
    purchase = c.get(prefix + "purchase", 0, 1000000);
    browse = c.get(prefix + "browse", 0, 1000000);
    refund = c.get(prefix + "refund", 0, 1000000);
    dup_refund = c.get(prefix + "dup_refund", 0, 1000000);
    bad = c.get(prefix + "bad", 0, 1000000);
  }

  WeightedPicker picker() const {
    return WeightedPicker({uint64_t(purchase), uint64_t(browse), uint64_t(refund), uint64_t(dup_refund), uint64_t(bad)});
  }
};

struct LoadParams {
  int64_t scenario;
  int64_t intents, quiet_intents;
  IntentWeights phase1, phase2;
  int64_t phase2_start_permille;
  int64_t k_purchase, k_browse_max;
  int64_t class_weight[rail::kSeatClassCount];
  int64_t pax_weight[rail::kMaxPassengers];
  int64_t window_permille, together_permille;
  int64_t same_line_permille;
  int64_t day_weight[rail::kDays];
  int64_t time_min, time_max;
  int64_t hot_lines, hot_permille, hot_stations, hot_inner_permille, hot_day, hot_time_min, hot_time_max;

  explicit LoadParams(Config& c)
      : scenario(c.get("scenario", 1, 3)),
        intents(c.get("intents", 0, 100000000)),
        quiet_intents(c.get("quiet_intents", 0, 100000000)),
        phase1(c, "w_"),
        phase2(c, "w2_") {
    phase2_start_permille = c.get("phase2_start_permille", 0, 1000);
    k_purchase = c.get("k_purchase", 1, rail::kMaxQueryResults);
    k_browse_max = c.get("k_browse_max", 1, rail::kMaxQueryResults);
    class_weight[0] = c.get("class_business", 0, 1000000);
    class_weight[1] = c.get("class_first", 0, 1000000);
    class_weight[2] = c.get("class_second", 0, 1000000);
    for (int i = 0; i < rail::kMaxPassengers; ++i) pax_weight[i] = c.get("pax" + std::to_string(i + 1), 0, 1000000);
    window_permille = c.get("window_permille", 0, 1000);
    together_permille = c.get("together_permille", 0, 1000);
    same_line_permille = c.get("same_line_permille", 0, 1000);
    for (int d = 0; d < rail::kDays; ++d) day_weight[d] = c.get("day" + std::to_string(d), 0, 1000000);
    time_min = c.get("time_min", 0, rail::kMinutesPerDay - 1);
    time_max = c.get("time_max", time_min, rail::kMinutesPerDay - 1);
    hot_lines = c.get("hot_lines", 0, 1000);
    hot_permille = c.get("hot_permille", 0, 1000);
    hot_stations = c.get("hot_stations", 1, rail::kMaxStopsPerTrain);
    hot_inner_permille = c.has("hot_inner_permille") ? c.get("hot_inner_permille", 0, 1000) : 0;
    hot_day = c.get("hot_day", 0, rail::kDays - 1);
    hot_time_min = c.get("hot_time_min", 0, rail::kMinutesPerDay - 1);
    hot_time_max = c.get("hot_time_max", hot_time_min, rail::kMinutesPerDay - 1);
    c.check_all_used();
  }
};

template <size_t N>
WeightedPicker picker_of(const int64_t (&w)[N]) {
  std::vector<uint64_t> v(w, w + N);
  return WeightedPicker(v);
}

class LoadGenerator {
 public:
  LoadGenerator(const World& world, const LoadParams& p, uint64_t seed)
      : world_(world),
        p_(p),
        rng_(seed),
        station_count_(static_cast<uint32_t>(world.station_names.size())),
        class_picker_(picker_of(p.class_weight)),
        pax_picker_(picker_of(p.pax_weight)),
        day_picker_(picker_of(p.day_weight)) {
    // Station popularity: Zipf-like weights over a ranking by size (lines x
    // trains served), ties broken at random. Big hubs draw the most demand.
    std::vector<uint64_t> size(station_count_, 0), lines_at(station_count_, 0);
    for (const auto& line : world.lines)
      for (uint32_t s : line.stations) ++lines_at[s];
    for (const auto& t : world.trains)
      for (const auto& stop : t.stops) ++size[stop.station];
    std::vector<uint32_t> rank(station_count_);
    std::iota(rank.begin(), rank.end(), 0);
    rng_.shuffle(rank);
    for (uint32_t s = 0; s < station_count_; ++s) size[s] *= lines_at[s];
    std::stable_sort(rank.begin(), rank.end(), [&](uint32_t a, uint32_t b) { return size[a] > size[b]; });
    std::vector<uint64_t> weights(station_count_);
    for (uint32_t i = 0; i < station_count_; ++i) weights[rank[i]] = 1000000 / (uint64_t(i) + 1) + 1;
    station_picker_ = WeightedPicker(weights);

    // Lines weighted by how many trains run on them; the busiest are hot.
    std::vector<uint64_t> trains_per_line(world.lines.size(), 0);
    for (const auto& t : world.trains) ++trains_per_line[t.line];
    line_picker_ = WeightedPicker(trains_per_line);
    std::vector<uint32_t> by_traffic(world.lines.size());
    std::iota(by_traffic.begin(), by_traffic.end(), 0);
    std::stable_sort(by_traffic.begin(), by_traffic.end(),
                     [&](uint32_t a, uint32_t b) { return trains_per_line[a] > trains_per_line[b]; });
    hot_lines_.assign(by_traffic.begin(),
                      by_traffic.begin() + std::min<size_t>(p.hot_lines, by_traffic.size()));

    // For building malformed books: which trains stop at each station.
    stops_at_.resize(station_count_);
    for (uint32_t t = 0; t < world.trains.size(); ++t) {
      const auto& stops = world.trains[t].stops;
      for (uint32_t i = 0; i < stops.size(); ++i) stops_at_[stops[i].station].push_back({t, i});
    }
  }

  Workload run() {
    Workload load;
    load.scenario = kScenarioNames[p_.scenario - 1];
    const WeightedPicker phase1 = p_.phase1.picker();
    const WeightedPicker phase2 = p_.phase2.picker();
    const int64_t phase2_start = p_.intents * p_.phase2_start_permille / 1000;
    load.intents.reserve(p_.intents);
    for (int64_t i = 0; i < p_.intents; ++i) {
      const size_t kind = (i < phase2_start ? phase1 : phase2).pick(rng_);
      load.intents.push_back(make_intent(kind, load));
    }
    load.quiet.reserve(p_.quiet_intents);
    for (int64_t i = 0; i < p_.quiet_intents; ++i) load.quiet.push_back(purchase(/*allow_hot=*/false));
    return load;
  }

 private:
  struct StopRef {
    uint32_t train, index;
  };

  Intent make_intent(size_t kind, Workload& load) {
    switch (kind) {
      case 0: return purchase(/*allow_hot=*/true);
      case 1: {
        Intent in;
        in.kind = IntentKind::Browse;
        in.query = random_query(false);
        in.query.k = static_cast<uint32_t>(rng_.range(1, p_.k_browse_max));
        return in;
      }
      case 2: {
        Intent in;
        in.kind = IntentKind::Refund;
        return in;
      }
      case 3: {
        Intent in;
        in.kind = IntentKind::DupRefund;
        return in;
      }
      default: return bad_intent(load);
    }
  }

  Intent purchase(bool allow_hot) {
    Intent in;
    in.kind = IntentKind::Purchase;
    const bool hot = allow_hot && !hot_lines_.empty() && rng_.chance(p_.hot_permille);
    in.query = random_query(hot);
    in.query.k = static_cast<uint32_t>(p_.k_purchase);
    in.seat_class = static_cast<uint8_t>(class_picker_.pick(rng_));
    in.passengers = static_cast<uint8_t>(pax_picker_.pick(rng_) + 1);
    for (int i = 0; i < in.passengers; ++i) {
      if (rng_.chance(p_.window_permille)) in.window_mask |= uint8_t(1u << i);
    }
    in.together = in.passengers > 1 && rng_.chance(p_.together_permille);
    return in;
  }

  rail::QueryRequest random_query(bool hot) {
    rail::QueryRequest q{};
    if (hot) {
      // Between the two ends of a hot line, in a narrow window of one day.
      const auto& st = world_.lines[hot_lines_[rng_.below(hot_lines_.size())]].stations;
      const int64_t span = std::min<int64_t>(p_.hot_stations, static_cast<int64_t>(st.size()) / 2);
      uint32_t head = st[rng_.below(span)];
      uint32_t tail = st[st.size() - 1 - rng_.below(span)];
      if (p_.hot_inner_permille && rng_.chance(p_.hot_inner_permille)) {
        // A shorter trip between two stations along the hot line.
        const uint64_t a = rng_.below(st.size() - 1);
        const uint64_t b = a + 1 + rng_.below(st.size() - 1 - a);
        head = st[a];
        tail = st[b];
      }
      const bool forward = rng_.chance(500);
      q.from = forward ? head : tail;
      q.to = forward ? tail : head;
      q.earliest = static_cast<int32_t>(p_.hot_day * rail::kMinutesPerDay + rng_.range(p_.hot_time_min, p_.hot_time_max));
      return q;
    }
    if (rng_.chance(p_.same_line_permille)) {
      const auto& st = world_.lines[line_picker_.pick(rng_)].stations;
      const uint64_t a = rng_.below(st.size());
      uint64_t b = rng_.below(st.size() - 1);
      if (b >= a) ++b;
      q.from = st[a];
      q.to = st[b];
    } else {
      q.from = static_cast<uint32_t>(station_picker_.pick(rng_));
      do {
        q.to = static_cast<uint32_t>(station_picker_.pick(rng_));
      } while (q.to == q.from);
    }
    const int64_t day = static_cast<int64_t>(day_picker_.pick(rng_));
    q.earliest = static_cast<int32_t>(day * rail::kMinutesPerDay + rng_.range(p_.time_min, p_.time_max));
    return q;
  }

  // A valid single leg on a random train.
  rail::LegRef random_leg() {
    const uint32_t t = static_cast<uint32_t>(rng_.below(world_.trains.size()));
    const auto& stops = world_.trains[t].stops;
    const uint64_t a = rng_.below(stops.size() - 1);
    const uint64_t b = a + 1 + rng_.below(stops.size() - 1 - a);
    return {t, static_cast<uint32_t>(rng_.below(rail::kDays)), stops[a].station, stops[b].station};
  }

  Intent bad_intent(Workload& load) {
    constexpr int kBadQueryKinds = 4, kBadBookKinds = 10;
    const int which = static_cast<int>(rng_.below(kBadQueryKinds + kBadBookKinds + 1));
    Intent in;
    if (which < kBadQueryKinds) {
      in.kind = IntentKind::BadQuery;
      in.query = random_query(false);
      in.query.k = static_cast<uint32_t>(rng_.range(1, rail::kMaxQueryResults));
      switch (which) {
        case 0: in.query.to = in.query.from; break;
        case 1: (rng_.chance(500) ? in.query.from : in.query.to) = station_count_ + uint32_t(rng_.below(1000)); break;
        case 2:
          in.query.earliest = rng_.chance(500) ? -1 - int32_t(rng_.below(100))
                                               : rail::kDays * rail::kMinutesPerDay + int32_t(rng_.below(100));
          break;
        default: in.query.k = rng_.chance(500) ? 0 : rail::kMaxQueryResults + 1; break;
      }
      return in;
    }
    if (which == kBadQueryKinds + kBadBookKinds) {
      in.kind = IntentKind::BadRefund;
      return in;
    }
    in.kind = IntentKind::BadBook;
    in.bad_book = static_cast<uint32_t>(load.bad_books.size());
    load.bad_books.push_back(bad_book(which - kBadQueryKinds));
    return in;
  }

  rail::BookRequest bad_book(int which) {
    rail::BookRequest b{};
    b.leg_count = 1;
    b.legs[0] = random_leg();
    b.seat_class = static_cast<rail::SeatClass>(class_picker_.pick(rng_));
    b.passenger_count = static_cast<uint32_t>(pax_picker_.pick(rng_) + 1);
    auto& leg = b.legs[0];
    switch (which) {
      case 0: leg.train = static_cast<uint32_t>(world_.trains.size() + rng_.below(1000)); break;
      case 1: leg.day = rail::kDays + static_cast<uint32_t>(rng_.below(3)); break;
      case 2: std::swap(leg.from, leg.to); break;
      case 3: {
        const auto& stops = world_.trains[leg.train].stops;
        uint32_t s;
        do {
          s = static_cast<uint32_t>(rng_.below(station_count_));
        } while (std::any_of(stops.begin(), stops.end(), [&](const rail::Stop& st) { return st.station == s; }));
        (rng_.chance(500) ? leg.from : leg.to) = s;
        break;
      }
      case 4: b.passenger_count = rng_.chance(500) ? 0 : rail::kMaxPassengers + 1; break;
      case 5: b.seat_class = static_cast<rail::SeatClass>(rail::kSeatClassCount + rng_.below(5)); break;
      case 6:
        if (rng_.chance(500)) {
          b.leg_count = 0;
        } else {
          b.leg_count = rail::kMaxLegs + 1;
          for (int i = 1; i < rail::kMaxLegs; ++i) b.legs[i] = random_leg();
        }
        break;
      case 7: {  // second leg does not start where the first one ends
        b.leg_count = 2;
        do {
          b.legs[1] = random_leg();
        } while (b.legs[1].from == leg.to);
        break;
      }
      case 8:  // same train-day twice (continuing on the same train)
        if (!continue_on_same_train(b)) b = bad_book(7);
        break;
      default:  // transfer shorter than the minimum
        if (!too_short_transfer(b)) b = bad_book(7);
        break;
    }
    return b;
  }

  bool continue_on_same_train(rail::BookRequest& b) {
    const auto& first = b.legs[0];
    const auto& stops = world_.trains[first.train].stops;
    size_t at = 0;
    while (stops[at].station != first.to) ++at;
    if (at + 1 >= stops.size()) return false;
    b.leg_count = 2;
    b.legs[1] = {first.train, first.day, first.to, stops[at + 1 + rng_.below(stops.size() - at - 1)].station};
    return true;
  }

  bool too_short_transfer(rail::BookRequest& b) {
    const auto& first = b.legs[0];
    const auto& stops = world_.trains[first.train].stops;
    int32_t arrive = 0;
    for (const auto& s : stops)
      if (s.station == first.to) arrive = int32_t(first.day) * rail::kMinutesPerDay + s.arrive;
    const auto& candidates = stops_at_[first.to];
    const size_t offset = rng_.below(candidates.size());
    for (size_t i = 0; i < candidates.size(); ++i) {
      const StopRef ref = candidates[(offset + i) % candidates.size()];
      if (ref.train == first.train) continue;
      const auto& next_stops = world_.trains[ref.train].stops;
      if (ref.index + 1 >= next_stops.size()) continue;
      // Latest day on which this train leaves too soon after our arrival.
      for (int day = rail::kDays - 1; day >= 0; --day) {
        const int32_t depart = day * rail::kMinutesPerDay + next_stops[ref.index].depart;
        if (depart >= arrive + rail::kMinTransferMinutes) continue;
        b.leg_count = 2;
        b.legs[1] = {ref.train, uint32_t(day), first.to,
                     next_stops[ref.index + 1 + rng_.below(next_stops.size() - ref.index - 1)].station};
        return true;
      }
    }
    return false;
  }

  const World& world_;
  const LoadParams& p_;
  Rng rng_;
  uint32_t station_count_;
  WeightedPicker station_picker_, line_picker_, class_picker_, pax_picker_, day_picker_;
  std::vector<uint32_t> hot_lines_;
  std::vector<std::vector<StopRef>> stops_at_;
};

}  // namespace

Workload generate_workload(const World& world, Config& cfg, uint64_t seed) {
  const LoadParams params(cfg);
  return LoadGenerator(world, params, seed).run();
}

}  // namespace railtools
