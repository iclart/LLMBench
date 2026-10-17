// railrun - single-threaded driver linked against one engine.
//
//   railrun query  <world> <load> <out.answers> [--random N SEED] [--limit N] [--expect FILE]
//       Runs the workload's queries (or N random ones) and writes one answer
//       line per query. With --expect, compares against a previous answer
//       file and reports the first difference.
//
//   railrun verify <world> <load> [--limit N]
//       Replays the workload on one thread and checks every result against an
//       independent seat model: request validation, seat validity, no
//       overlapping sales, exact false-negative detection, refund semantics.
//       Reports seat-preference satisfaction.
//
//   railrun tickets <world> <load> [--limit N]
//       Same replay and checks as verify, printing every ticket issued (train,
//       times, stations, seats per leg), failed purchase and refund, for the
//       first N intents of the main phase.
//
//   railrun func <world> <suite> [--from N] [--trace FILE] [--json FILE] [--record OUT]
//       Runs the functional test suite (suite.h) and prints the pass rate per
//       category and the first failures. --from starts at case N, --trace
//       appends one line per case to FILE as it goes (for drivers that
//       restart after a crash), --record stores this engine's query answers
//       as the expected ones.
#include <chrono>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_set>
#include <vector>

#include "answers.h"
#include "quality.h"
#include "rng.h"
#include "rules.h"
#include "seats.h"
#include "suite.h"
#include "ticket_system.h"
#include "workload.h"
#include "world.h"

namespace {

using namespace railtools;
using Clock = std::chrono::steady_clock;

struct Options {
  std::string mode, world, load, out, expect, json, record, trace;
  uint64_t random_count = 0, random_seed = 0;
  uint64_t limit = UINT64_MAX, from = 0;
};

[[noreturn]] void usage() {
  std::fprintf(stderr,
               "usage:\n"
               "  railrun query  <world> <load> <out.answers> [--random N SEED] [--limit N] [--expect FILE]\n"
               "  railrun verify <world> <load> [--limit N]\n"
               "  railrun tickets <world> <load> [--limit N]\n"
               "  railrun func   <world> <suite> [--from N] [--trace FILE] [--json FILE] [--record OUT]\n");
  std::exit(2);
}

Options parse_args(int argc, char** argv) {
  Options o;
  std::vector<std::string> pos;
  for (int i = 1; i < argc; ++i) {
    const std::string a = argv[i];
    if (a == "--random" && i + 2 < argc) {
      o.random_count = std::stoull(argv[++i]);
      o.random_seed = std::stoull(argv[++i]);
    } else if (a == "--limit" && i + 1 < argc) {
      o.limit = std::stoull(argv[++i]);
    } else if (a == "--expect" && i + 1 < argc) {
      o.expect = argv[++i];
    } else if (a == "--json" && i + 1 < argc) {
      o.json = argv[++i];
    } else if (a == "--record" && i + 1 < argc) {
      o.record = argv[++i];
    } else if (a == "--from" && i + 1 < argc) {
      o.from = std::stoull(argv[++i]);
    } else if (a == "--trace" && i + 1 < argc) {
      o.trace = argv[++i];
    } else if (a.rfind("--", 0) == 0) {
      usage();
    } else {
      pos.push_back(a);
    }
  }
  if (pos.empty()) usage();
  o.mode = pos[0];
  if (o.mode == "query" && pos.size() == 4) {
    o.world = pos[1], o.load = pos[2], o.out = pos[3];
  } else if ((o.mode == "verify" || o.mode == "tickets" || o.mode == "func") && pos.size() == 3) {
    o.world = pos[1], o.load = pos[2];
  } else {
    usage();
  }
  return o;
}

double seconds_since(Clock::time_point t) { return std::chrono::duration<double>(Clock::now() - t).count(); }

// ---------------------------------------------------------------- query mode

// Random queries for cross-checking: half uniform, half built from a random
// 1-3 leg ride so that multi-leg answers are common.
std::vector<rail::QueryRequest> random_queries(const World& w, uint64_t count, uint64_t seed) {
  Rng rng(seed);
  std::vector<std::vector<std::pair<uint32_t, uint32_t>>> at(w.station_names.size());
  for (uint32_t t = 0; t < w.trains.size(); ++t)
    for (uint32_t i = 0; i < w.trains[t].stops.size(); ++i) at[w.trains[t].stops[i].station].push_back({t, i});
  const uint32_t n = static_cast<uint32_t>(w.station_names.size());
  std::vector<rail::QueryRequest> qs;
  while (qs.size() < count) {
    rail::QueryRequest q{};
    q.from = static_cast<uint32_t>(rng.below(n));
    if (rng.chance(500)) {
      q.to = static_cast<uint32_t>(rng.below(n));
    } else {
      uint32_t s = q.from;
      const int64_t legs = rng.range(1, rail::kMaxLegs);
      for (int64_t l = 0; l < legs; ++l) {
        const auto [t, i] = at[s][rng.below(at[s].size())];
        const auto& stops = w.trains[t].stops;
        if (i + 1 >= stops.size()) break;
        s = stops[i + 1 + rng.below(stops.size() - i - 1)].station;
      }
      q.to = s;
    }
    if (q.to == q.from) continue;
    q.earliest = static_cast<int32_t>(rng.below(rail::kDays * rail::kMinutesPerDay));
    q.k = static_cast<uint32_t>(rng.range(1, rail::kMaxQueryResults));
    qs.push_back(q);
  }
  return qs;
}

int run_query_mode(const Options& o, const World& world, rail::TicketSystem& sys) {
  std::vector<rail::QueryRequest> qs;
  if (o.random_count) {
    qs = random_queries(world, o.random_count, o.random_seed);
  } else {
    const Workload load = load_workload(o.load);
    for (const auto* phase : {&load.intents, &load.quiet}) {
      for (const auto& in : *phase) {
        if (in.kind == IntentKind::Purchase || in.kind == IntentKind::Browse || in.kind == IntentKind::BadQuery) {
          qs.push_back(in.query);
        }
      }
    }
  }
  if (qs.size() > o.limit) qs.resize(o.limit);

  std::string out;
  double busy = 0;
  for (const auto& q : qs) {
    rail::QueryResult r{};
    const auto t0 = Clock::now();
    const rail::Status st = sys.query(q, &r);
    busy += seconds_since(t0);
    append_answer(out, st, r);
    out += '\n';
  }
  std::fprintf(stderr, "%zu queries in %.3f s (%.1f us/query)\n", qs.size(), busy, busy * 1e6 / double(qs.size()));
  {
    std::ofstream f(o.out, std::ios::binary);
    f << out;
    if (!f) throw std::runtime_error("cannot write " + o.out);
  }
  if (o.expect.empty()) return 0;

  std::ifstream ef(o.expect, std::ios::binary);
  if (!ef) throw std::runtime_error("cannot read " + o.expect);
  std::istringstream got(out);
  std::string a, b;
  for (size_t line = 0;; ++line) {
    const bool has_a = static_cast<bool>(std::getline(got, a));
    const bool has_b = static_cast<bool>(std::getline(ef, b));
    if (!has_a && !has_b) break;
    if (has_a != has_b || a != b) {
      std::fprintf(stderr, "MISMATCH at query %zu\n", line);
      if (line < qs.size()) {
        const auto& q = qs[line];
        std::fprintf(stderr, "  request  from=%u to=%u earliest=%d k=%u\n", q.from, q.to, q.earliest, q.k);
      }
      std::fprintf(stderr, "  got      %s\n  expected %s\n", has_a ? a.c_str() : "<none>", has_b ? b.c_str() : "<none>");
      return 1;
    }
  }
  std::fprintf(stderr, "all %zu answers match %s\n", qs.size(), o.expect.c_str());
  return 0;
}

// ---------------------------------------------------------------- verify mode

struct VerifyError : std::runtime_error {
  using std::runtime_error::runtime_error;
};

struct Stats {
  uint64_t queries = 0, books = 0, sold_out = 0, withheld = 0, refunds = 0, invalid = 0;
  uint64_t purchases = 0, purchases_ok = 0;
  Quality quality;  // purchases, satisfaction and utilization (main phase)
  uint64_t window_wanted = 0, window_got = 0, together_wanted = 0, together_got = 0;
  double busy = 0;
};

class Verifier {
 public:
  Verifier(const World& world, rail::TicketSystem& sys) : w_(world), sys_(sys), rng_(0x5eed), occ_(world.trains.size() * rail::kDays) {}

  void run(const Workload& load, const std::vector<Intent>& intents, uint64_t limit, Stats& st) {
    for (uint64_t i = 0; i < intents.size() && i < limit; ++i) {
      where_ = i;
      play(load, intents[i], st);
    }
  }

  void set_phase(const char* phase) { phase_ = phase; }

  // Utilization of the turned-down (train-day, class) pairs, from the seats held now.
  void measure(const World& w, Stats& st) {
    st.quality.sold_out = st.sold_out;
    st.quality.withheld = st.withheld;
    st.quality.measure(w, [&](uint32_t td, int seat) { return occ_[td].empty() ? uint64_t(0) : occ_[td][seat]; });
  }
  void print_tickets(FILE* out) { tickets_ = out; }

 private:
  struct Hold {
    size_t td;
    int seat;
    uint64_t mask;
  };
  struct LiveOrder {
    uint64_t id;
    std::vector<Hold> holds;
  };

  [[noreturn]] void fail(const std::string& msg) const {
    throw VerifyError(std::string(phase_) + " intent " + std::to_string(where_) + ": " + msg);
  }

  rail::Status timed_query(const rail::QueryRequest& q, rail::QueryResult* r, Stats& st) {
    const auto t0 = Clock::now();
    const rail::Status s = sys_.query(q, r);
    st.busy += seconds_since(t0);
    ++st.queries;
    return s;
  }

  rail::Status timed_book(const rail::BookRequest& b, rail::BookResult* r, Stats& st) {
    const auto t0 = Clock::now();
    const rail::Status s = sys_.book(b, r);
    st.busy += seconds_since(t0);
    ++st.books;
    return s;
  }

  rail::Status timed_refund(uint64_t id, Stats& st) {
    const auto t0 = Clock::now();
    const rail::Status s = sys_.refund(id);
    st.busy += seconds_since(t0);
    ++st.refunds;
    return s;
  }

  void play(const Workload& load, const Intent& in, Stats& st) {
    switch (in.kind) {
      case IntentKind::Purchase: {
        ++st.purchases;
        const uint64_t ok_before = st.purchases_ok, withheld_before = st.withheld;
        rail::QueryResult r{};
        const rail::Status s = timed_query(in.query, &r, st);
        check_query(in.query, s, r);
        for (uint32_t i = 0; i < r.count; ++i) {
          rail::BookRequest b{};
          b.leg_count = r.items[i].leg_count;
          for (uint32_t j = 0; j < b.leg_count; ++j) {
            const auto& l = r.items[i].legs[j];
            b.legs[j] = {l.train, l.day, l.from, l.to};
          }
          b.seat_class = static_cast<rail::SeatClass>(in.seat_class);
          b.passenger_count = in.passengers;
          for (int p = 0; p < rail::kMaxPassengers; ++p) b.window[p] = (in.window_mask >> p) & 1;
          b.together = in.together;
          if (book_and_check(b, st)) {
            ++st.purchases_ok;
            break;
          }
        }
        if (r.count) st.quality.add_purchase(in.passengers, r.items[0], st.purchases_ok > ok_before);
        if (tickets_ && st.purchases_ok == ok_before) {
          const uint64_t withheld = st.withheld - withheld_before;
          std::string why = "no itinerary";
          if (r.count) {
            why = std::to_string(r.count) + " itineraries";
            if (withheld == r.count) why += ", all withheld";
            else if (withheld) why += ", " + std::to_string(withheld) + " withheld, the rest sold out";
            else why += ", all sold out";
          }
          std::fprintf(tickets_, "#%llu  purchase failed: %s\n", (unsigned long long)where_, why.c_str());
        }
        break;
      }
      case IntentKind::Browse:
      case IntentKind::BadQuery: {
        rail::QueryResult r{};
        const rail::Status s = timed_query(in.query, &r, st);
        check_query(in.query, s, r);
        break;
      }
      case IntentKind::BadBook:
        book_and_check(load.bad_books[in.bad_book], st);
        break;
      case IntentKind::Refund: {
        if (live_.empty()) break;
        const size_t pick = rng_.below(live_.size());
        LiveOrder order = std::move(live_[pick]);
        live_[pick] = std::move(live_.back());
        live_.pop_back();
        if (timed_refund(order.id, st) != rail::Status::Ok) fail("refund of live order " + std::to_string(order.id) + " was not Ok");
        for (const Hold& h : order.holds) occ_[h.td][h.seat] &= ~h.mask;
        refunded_.push_back(order.id);
        if (tickets_) std::fprintf(tickets_, "#%llu  refund of order %llu\n", (unsigned long long)where_, (unsigned long long)order.id);
        break;
      }
      case IntentKind::DupRefund: {
        if (refunded_.empty()) break;
        const uint64_t id = refunded_[rng_.below(refunded_.size())];
        if (timed_refund(id, st) != rail::Status::NotFound) fail("second refund of order " + std::to_string(id) + " was not NotFound");
        break;
      }
      case IntentKind::BadRefund:
        if (timed_refund(0, st) != rail::Status::NotFound) fail("refund of order 0 was not NotFound");
        break;
    }
  }

  void check_query(const rail::QueryRequest& q, rail::Status s, const rail::QueryResult& r) {
    if (!is_valid_query(w_, q)) {
      if (s != rail::Status::Invalid) fail("malformed query not rejected as Invalid");
      return;
    }
    if (s != rail::Status::Ok) fail("valid query returned status " + std::to_string(int(s)));
    if (r.count > q.k) fail("query returned more than k itineraries");
    const int32_t window_end = (q.earliest / rail::kMinutesPerDay + 1) * rail::kMinutesPerDay;
    for (uint32_t i = 0; i < r.count; ++i) {
      const auto& it = r.items[i];
      if (it.leg_count < 1 || it.leg_count > static_cast<uint32_t>(rail::kMaxLegs)) fail("itinerary with bad leg count");
      rail::LegRef refs[rail::kMaxLegs];
      for (uint32_t j = 0; j < it.leg_count; ++j) {
        const auto& l = it.legs[j];
        refs[j] = {l.train, l.day, l.from, l.to};
        ResolvedLeg rl;
        if (!resolve_leg(w_, refs[j], &rl) || rl.depart != l.depart || rl.arrive != l.arrive) {
          fail("itinerary leg with wrong times or stations");
        }
      }
      if (!is_valid_itinerary(w_, refs, it.leg_count)) fail("invalid itinerary returned");
      if (refs[0].from != q.from || refs[it.leg_count - 1].to != q.to) fail("itinerary does not connect from and to");
      if (it.legs[0].depart < q.earliest || it.legs[0].depart >= window_end) fail("first leg outside the departure window");
    }
  }

  // Returns true when the book succeeded.
  bool book_and_check(const rail::BookRequest& b, Stats& st) {
    rail::BookResult res{};
    const rail::Status s = timed_book(b, &res, st);
    if (!is_valid_book(w_, b)) {
      if (s != rail::Status::Invalid) fail("malformed book not rejected as Invalid");
      ++st.invalid;
      return false;
    }
    if (s == rail::Status::Invalid) fail("valid book rejected as Invalid");
    const rail::SeatClass cls = b.seat_class;
    const ClassInfo& ci = class_info(cls);
    const int n = static_cast<int>(b.passenger_count);

    ResolvedLeg legs[rail::kMaxLegs];
    for (uint32_t j = 0; j < b.leg_count; ++j) resolve_leg(w_, b.legs[j], &legs[j]);
    auto mask_of = [](const ResolvedLeg& l) {
      return ((uint64_t(1) << l.to_index) - 1) & ~((uint64_t(1) << l.from_index) - 1);
    };

    if (s == rail::Status::SoldOut) {
      bool all_enough = true;
      for (uint32_t j = 0; j < b.leg_count; ++j) {
        const auto& seats = seats_of(b.legs[j].train, b.legs[j].day);
        const uint64_t mask = mask_of(legs[j]);
        int free = 0;
        for (int k = ci.first_seat; k < ci.first_seat + ci.seats(); ++k) free += (seats[k] & mask) == 0;
        all_enough &= free >= n;
        if (free < n) st.quality.unmet.insert({b.legs[j].train * rail::kDays + b.legs[j].day, int(cls)});
      }
      if (all_enough) fail("false negative: SoldOut although every leg had enough free seats");
      ++st.sold_out;
      return false;
    }
    if (s == rail::Status::Withheld) {  // allowed: the engine's sales policy
      for (uint32_t j = 0; j < b.leg_count; ++j)
        st.quality.unmet.insert({b.legs[j].train * rail::kDays + b.legs[j].day, int(cls)});
      ++st.withheld;
      return false;
    }
    if (s != rail::Status::Ok) fail("book returned status " + std::to_string(int(s)));

    if (res.order_id == 0 || !issued_.insert(res.order_id).second) fail("order id is zero or reused");
    LiveOrder order{res.order_id, {}};
    for (uint32_t j = 0; j < b.leg_count; ++j) {
      const size_t td = size_t(b.legs[j].train) * rail::kDays + b.legs[j].day;
      auto& seats = seats_of(b.legs[j].train, b.legs[j].day);
      const uint64_t mask = mask_of(legs[j]);
      for (int p = 0; p < n; ++p) {
        const rail::Seat& seat = res.seats[j][p];
        const int idx = seat_index(cls, seat);
        if (idx < 0) fail("seat " + seat_name(seat) + " is not a seat of the requested class");
        if (seats[idx] & mask) fail("seat " + seat_name(seat) + " sold twice for overlapping intervals");
        seats[idx] |= mask;
        order.holds.push_back({td, idx, mask});
        if (b.window[p]) {
          ++st.window_wanted;
          st.window_got += is_window(seat);
        }
      }
      if (b.together && n >= 2) {
        ++st.together_wanted;
        st.together_got += seated_together(cls, res.seats[j], n);
      }
    }
    live_.push_back(std::move(order));
    if (tickets_) print_ticket(b, res, legs);
    return true;
  }

  static std::string clock_of(int32_t t) {
    char buf[32];
    std::snprintf(buf, sizeof buf, "day %d %02d:%02d", t / rail::kMinutesPerDay, (t % rail::kMinutesPerDay) / 60,
                  t % 60);
    return buf;
  }

  void print_ticket(const rail::BookRequest& b, const rail::BookResult& res, const ResolvedLeg* legs) const {
    static const char* kClassNames[] = {"Business", "First", "Second"};
    const int n = static_cast<int>(b.passenger_count);
    std::string wants;
    for (int p = 0; p < n; ++p)
      if (b.window[p]) wants += (wants.empty() ? " window:" : ",") + std::to_string(p + 1);
    if (b.together && n >= 2) wants += " together";
    std::fprintf(tickets_, "#%llu  order %llu  %s x%d%s%s\n", (unsigned long long)where_,
                 (unsigned long long)res.order_id, kClassNames[static_cast<int>(b.seat_class)], n,
                 wants.empty() ? "" : "  wants", wants.c_str());
    for (uint32_t j = 0; j < b.leg_count; ++j) {
      const auto& l = b.legs[j];
      std::string seats;
      for (int p = 0; p < n; ++p) seats += " " + seat_name(res.seats[j][p]);
      std::fprintf(tickets_, "    %-8s %s %s -> %s %s  %s\n", w_.trains[l.train].name.c_str(),
                   w_.station_names[l.from].c_str(), clock_of(legs[j].depart).c_str(), w_.station_names[l.to].c_str(),
                   clock_of(legs[j].arrive).c_str(), seats.c_str());
    }
  }

  std::vector<uint64_t>& seats_of(uint32_t train, uint32_t day) {
    auto& v = occ_[size_t(train) * rail::kDays + day];
    if (v.empty()) v.assign(rail::kSeatsPerTrainDay, 0);
    return v;
  }

  const World& w_;
  rail::TicketSystem& sys_;
  Rng rng_;
  std::vector<std::vector<uint64_t>> occ_;  // [train-day][seat] occupied stop intervals
  std::vector<LiveOrder> live_;
  std::vector<uint64_t> refunded_;
  std::unordered_set<uint64_t> issued_;
  const char* phase_ = "";
  uint64_t where_ = 0;
  FILE* tickets_ = nullptr;
};

void print_stats(const char* phase, const Stats& s, bool quality) {
  auto pct = [](uint64_t a, uint64_t b) { return b ? 100.0 * double(a) / double(b) : 0.0; };
  std::printf("%s: %llu queries, %llu books (%llu sold out, %llu withheld, %llu invalid), %llu refunds, %.3f s in engine\n",
              phase, (unsigned long long)s.queries, (unsigned long long)s.books, (unsigned long long)s.sold_out,
              (unsigned long long)s.withheld, (unsigned long long)s.invalid, (unsigned long long)s.refunds, s.busy);
  std::printf("  purchases %llu, succeeded %.1f%%; window %.1f%% of %llu; together %.1f%% of %llu\n",
              (unsigned long long)s.purchases, pct(s.purchases_ok, s.purchases), pct(s.window_got, s.window_wanted),
              (unsigned long long)s.window_wanted, pct(s.together_got, s.together_wanted),
              (unsigned long long)s.together_wanted);
  if (quality && s.quality.purchases) {
    const double u = s.quality.utilization();
    std::printf("  satisfaction %.2f%%; %lld passenger-minutes carried (%.2f%% of those asked for); "
                "utilization %s over %zu turned-down train-day classes\n",
                100 * s.quality.satisfaction(), (long long)s.quality.passenger_minutes,
                100 * s.quality.transport_share(), u < 0 ? "n/a" : (std::to_string(100 * u).substr(0, 5) + "%").c_str(),
                s.quality.unmet.size());
  }
}

int run_verify_mode(const Options& o, const World& world, rail::TicketSystem& sys) {
  const Workload load = load_workload(o.load);
  Verifier v(world, sys);
  Stats main_stats, quiet_stats;
  const bool tickets = o.mode == "tickets";
  if (tickets) v.print_tickets(stdout);
  try {
    v.set_phase("main");
    v.run(load, load.intents, o.limit, main_stats);
    v.measure(world, main_stats);
    if (!tickets) {
      v.set_phase("quiet");
      v.run(load, load.quiet, o.limit, quiet_stats);
    }
  } catch (const VerifyError& e) {
    std::printf("FAILED: %s\n", e.what());
    return 1;
  }
  print_stats("main ", main_stats, true);
  if (!tickets) print_stats("quiet", quiet_stats, false);
  std::printf("OK\n");
  return 0;
}

// ---------------------------------------------------------------- func mode

std::string json_string(const std::string& s) {
  std::string out = "\"";
  for (char c : s) {
    if (c == '"' || c == '\\') out += '\\', out += c;
    else if (c == '\n') out += "\\n";
    else if (static_cast<unsigned char>(c) < 0x20) out += ' ';
    else out += c;
  }
  return out + "\"";
}

int run_func_mode(const Options& o, const World& world, rail::TicketSystem& sys) {
  std::vector<SuiteCase> cases = load_suite(o.load);
  SuiteRunner runner(world, sys);
  struct Tally {
    uint64_t passed = 0, total = 0;
    std::vector<std::string> failures;
  };
  std::vector<std::pair<std::string, Tally>> tallies;  // in order of first appearance
  auto tally = [&](const std::string& cat) -> Tally& {
    for (auto& [name, t] : tallies)
      if (name == cat) return t;
    tallies.push_back({cat, {}});
    return tallies.back().second;
  };
  for (const auto& c : cases) tally(c.category);
  const bool record = !o.record.empty();
  FILE* trace = o.trace.empty() ? nullptr : std::fopen(o.trace.c_str(), "a");
  if (!o.trace.empty() && !trace) throw std::runtime_error("cannot write " + o.trace);
  for (size_t i = o.from; i < cases.size(); ++i) {
    const std::string err = runner.run(cases[i], record);
    Tally& t = tally(cases[i].category);
    ++t.total;
    t.passed += err.empty();
    if (!err.empty() && t.failures.size() < 3) t.failures.push_back("case " + std::to_string(i) + ", " + err);
    if (trace) {
      std::fprintf(trace, "%zu %s %s\n", i, err.empty() ? "ok" : "fail", err.empty() ? "" : json_string(err).c_str());
      std::fflush(trace);
    }
  }
  if (trace) std::fclose(trace);
  if (record) {
    std::ofstream f(o.record, std::ios::binary);
    write_suite(f, cases);
    if (!f) throw std::runtime_error("cannot write " + o.record);
    std::fprintf(stderr, "recorded %zu cases into %s\n", cases.size(), o.record.c_str());
    return 0;
  }
  double rate_sum = 0;
  int rated = 0;
  std::printf("functional suite: %zu cases\n", cases.size() - std::min<size_t>(o.from, cases.size()));
  for (const auto& [name, t] : tallies) {
    if (!t.total) continue;
    rate_sum += double(t.passed) / double(t.total);
    ++rated;
    std::printf("  %-16s %4llu / %-4llu%s\n", name.c_str(), (unsigned long long)t.passed,
                              (unsigned long long)t.total, t.passed == t.total ? "" : "  FAILED");
  }
  bool any_failure = false;
  for (const auto& [name, t] : tallies) {
    for (const auto& f : t.failures) {
      if (!any_failure) std::printf("first failures:\n");
      any_failure = true;
      std::printf("  [%s] %s\n", name.c_str(), f.c_str());
    }
  }
  std::printf("functional score: %.1f (mean pass rate of %d categories)\n", rated ? 100 * rate_sum / rated : 0.0, rated);
  if (!o.json.empty()) {
    std::string j = "{\"categories\": {";
    bool first = true;
    for (const auto& [name, t] : tallies) {
      j += (first ? "" : ", ") + json_string(name) + ": {\"passed\": " + std::to_string(t.passed) +
           ", \"total\": " + std::to_string(t.total) + ", \"failures\": [";
      for (size_t k = 0; k < t.failures.size(); ++k) j += (k ? ", " : "") + json_string(t.failures[k]);
      j += "]}";
      first = false;
    }
    j += "}}\n";
    std::ofstream f(o.json, std::ios::binary);
    f << j;
  }
  return any_failure ? 1 : 0;
}

}  // namespace

int main(int argc, char** argv) {
  const Options o = parse_args(argc, argv);
  try {
    const World world = load_world(o.world);
    validate_world(world);
    const WorldView view(world);
    std::unique_ptr<rail::TicketSystem> sys(rail::create_ticket_system());
    const auto t0 = Clock::now();
    sys->init(view.view());
    std::fprintf(stderr, "init %.3f s\n", seconds_since(t0));
    if (o.mode == "query") return run_query_mode(o, world, *sys);
    if (o.mode == "func") return run_func_mode(o, world, *sys);
    return run_verify_mode(o, world, *sys);
  } catch (const std::exception& e) {
    std::fprintf(stderr, "railrun: %s\n", e.what());
    return 1;
  }
}
