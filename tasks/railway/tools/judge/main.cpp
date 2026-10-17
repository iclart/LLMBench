// railjudge - checks a railbench log and computes the run's metrics.
//
//   railjudge <world> <load> <reference.answers> <bench.log> [--json OUT] [--max-errors N]
//
// The reference answers are railrun's `query` output for the same workload
// (any engine whose answers match the oracle). Every check follows TASK.md
// and judges concurrent calls by linearizability using the logged time
// windows, giving the engine the benefit of the doubt where the order of
// overlapping calls is unknown:
//
// - query: the answer must equal the reference answer byte for byte.
// - book: Invalid exactly for malformed requests; Withheld (a sales policy)
//   is allowed for any valid request; seats of an Ok order must
//   exist, belong to the class and be distinct within a leg; order ids are
//   nonzero and unique.
// - oversell: two orders holding the same seat over overlapping intervals
//   are an error unless one could have been refunded before the other was
//   booked.
// - false negative: SoldOut is an error if every leg certainly had enough
//   free seats at every moment of the call. In the single-threaded quiet and
//   audit phases this is exact; the audit re-books intervals of failed and
//   refunded orders until SoldOut, so seats an engine lost become visible.
// - refund: at most one Ok per order, Ok only for issued orders, NotFound only
//   when the order may already have been refunded (or never existed).
//
// Exit status 0 = all checks passed, 1 = violations, 2 = usage or I/O error.
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <map>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "answers.h"
#include "quality.h"
#include "benchlog.h"
#include "rules.h"
#include "seats.h"
#include "workload.h"
#include "world.h"

namespace {

using namespace railtools;

constexpr uint64_t kNever = UINT64_MAX;

struct Errors {
  size_t max_kept = 20;
  std::vector<std::string> kept;
  std::map<std::string, uint64_t> by_kind;

  void add(const std::string& kind, const std::string& msg) {
    ++by_kind[kind];
    if (kept.size() < max_kept) kept.push_back(kind + ": " + msg);
  }
  bool any() const { return !by_kind.empty(); }
};

struct LegInfo {
  uint32_t td;    // train * kDays + day
  uint64_t mask;  // occupied segments
};

struct Order {
  uint64_t book_start, book_end;
  Phase phase;
  uint64_t refund_start = kNever, refund_end = kNever;  // of the successful refund
  uint32_t leg_count = 0;
  LegInfo legs[rail::kMaxLegs];
};

struct Hold {
  uint32_t order;
  uint64_t mask;
};

struct SoldOut {
  uint64_t start, end;
  rail::SeatClass cls;
  uint32_t passengers;
  uint32_t leg_count;
  LegInfo legs[rail::kMaxLegs];
  const Event* ev;
};

struct Percentiles {
  uint64_t count = 0, p50 = 0, p99 = 0, max = 0;
};

Percentiles percentiles(std::vector<uint32_t>& v) {
  Percentiles p;
  p.count = v.size();
  if (v.empty()) return p;
  auto at = [&](double q) {
    const size_t k = std::min(v.size() - 1, static_cast<size_t>(q * double(v.size())));
    std::nth_element(v.begin(), v.begin() + static_cast<std::ptrdiff_t>(k), v.end());
    return uint64_t(v[k]);
  };
  p.p50 = at(0.50);
  p.p99 = at(0.99);
  p.max = *std::max_element(v.begin(), v.end());
  return p;
}

class Judge {
 public:
  Judge(const World& w, const Workload& load, std::vector<Answer> answers, const BenchLog& log, Errors& errors)
      : w_(w), load_(load), answers_(std::move(answers)), log_(log), err_(errors) {}

  void run() {
    map_answers();
    seats_.resize(w_.trains.size() * rail::kDays);
    for (const Event& e : log_.events) {
      switch (e.op) {
        case Op::Query: check_query(e); break;
        case Op::Book: check_book(e); break;
        case Op::Refund: break;  // after all orders are known
        default: err_.add("log", "unknown op"); break;
      }
    }
    for (const Event& e : log_.events)
      if (e.op == Op::Refund) collect_refund(e);
    check_refunds();
    check_coverage();
    check_oversell();
    check_false_negatives();
    measure_quality();
  }

  // ---- metrics
  // Indexed by Phase.
  uint64_t window_wanted[3] = {}, window_got[3] = {}, together_wanted[3] = {}, together_got[3] = {};
  uint64_t books_ok[3] = {}, sold_out[3] = {}, withheld[3] = {};
  Quality quality;  // main phase (quality.h)

 private:
  void map_answers() {
    const auto main_q = query_ordinals(load_, false);
    const auto quiet_q = query_ordinals(load_, true);
    if (answers_.size() != main_q.size() + quiet_q.size()) {
      throw std::runtime_error("reference answers do not match the workload (" + std::to_string(answers_.size()) +
                               " answers, " + std::to_string(main_q.size() + quiet_q.size()) + " queries)");
    }
    answer_of_[0].assign(load_.intents.size(), -1);
    answer_of_[1].assign(load_.quiet.size(), -1);
    for (size_t i = 0; i < main_q.size(); ++i) answer_of_[0][main_q[i]] = int64_t(i);
    for (size_t i = 0; i < quiet_q.size(); ++i) answer_of_[1][quiet_q[i]] = int64_t(main_q.size() + i);
  }

  const Intent* intent_of(const Event& e) {
    if (e.phase == Phase::Audit) {
      err_.add("log", "audit phase may only book");
      return nullptr;
    }
    if (e.phase != Phase::Main && e.phase != Phase::Quiet) {
      err_.add("log", "unknown phase");
      return nullptr;
    }
    const auto& intents = e.phase == Phase::Main ? load_.intents : load_.quiet;
    if (e.intent >= intents.size()) {
      err_.add("log", "event refers to intent " + std::to_string(e.intent) + " beyond the workload");
      return nullptr;
    }
    return &intents[e.intent];
  }

  std::string where(const Event& e) const {
    if (e.phase == Phase::Audit) return "audit probe " + std::to_string(e.intent);
    return std::string(e.phase == Phase::Main ? "main" : "quiet") + " intent " + std::to_string(e.intent);
  }

  bool audit_request(const Event& e, rail::BookRequest* b) {
    if (e.intent >= log_.probes.size() || e.attempt < 1 || e.attempt > rail::kMaxPassengers) {
      err_.add("log", "malformed audit event");
      return false;
    }
    const Probe& p = log_.probes[e.intent];
    *b = rail::BookRequest{};
    b->leg_count = 1;
    b->legs[0] = p.leg;
    b->seat_class = static_cast<rail::SeatClass>(p.seat_class);
    b->passenger_count = e.attempt;
    return true;
  }

  void check_query(const Event& e) {
    const Intent* in = intent_of(e);
    if (!in) return;
    const int64_t a = answer_of_[int(e.phase)][e.intent];
    if (a < 0) {
      err_.add("log", where(e) + " issued a query it should not have");
      return;
    }
    ++queries_seen_[int(e.phase)];
    if (e.value != answers_[a].hash) err_.add("query", where(e) + ": answer differs from the reference");
  }

  // Rebuilds the book request the harness sent, from the reference answer.
  bool request_of(const Event& e, const Intent& in, rail::BookRequest* b) {
    if (e.attempt == kBadBookAttempt) {
      if (in.kind != IntentKind::BadBook) return false;
      *b = load_.bad_books[in.bad_book];
      return true;
    }
    if (in.kind != IntentKind::Purchase) return false;
    const Answer& ans = answers_[answer_of_[int(e.phase)][e.intent]];
    // A wrong query answer is already reported; its books cannot be judged.
    if (e.attempt >= ans.items.size()) return false;
    const rail::Itinerary& it = ans.items[e.attempt];
    *b = rail::BookRequest{};
    b->leg_count = it.leg_count;
    for (uint32_t j = 0; j < it.leg_count; ++j) {
      b->legs[j] = {it.legs[j].train, it.legs[j].day, it.legs[j].from, it.legs[j].to};
    }
    b->seat_class = static_cast<rail::SeatClass>(in.seat_class);
    b->passenger_count = in.passengers;
    for (int p = 0; p < rail::kMaxPassengers; ++p) b->window[p] = (in.window_mask >> p) & 1;
    b->together = in.together;
    return true;
  }

  void check_book(const Event& e) {
    rail::BookRequest b;
    const Intent* in = nullptr;
    if (e.phase == Phase::Audit) {
      if (!audit_request(e, &b)) return;
    } else if (!(in = intent_of(e))) {
      return;
    } else if (!request_of(e, *in, &b)) {
      if (in->kind != IntentKind::Purchase && in->kind != IntentKind::BadBook) {
        err_.add("log", where(e) + " issued a book it should not have");
      }
      return;
    }
    const auto st = static_cast<rail::Status>(e.status);
    const bool valid = is_valid_book(w_, b);
    if (!valid) {
      if (st != rail::Status::Invalid) err_.add("book", where(e) + ": malformed request not rejected as Invalid");
      return;
    }
    if (st == rail::Status::Invalid) {
      err_.add("book", where(e) + ": valid request rejected as Invalid");
      return;
    }
    LegInfo legs[rail::kMaxLegs];
    for (uint32_t j = 0; j < b.leg_count; ++j) {
      ResolvedLeg rl;
      resolve_leg(w_, b.legs[j], &rl);
      legs[j].td = b.legs[j].train * rail::kDays + b.legs[j].day;
      legs[j].mask = ((uint64_t(1) << rl.to_index) - 1) & ~((uint64_t(1) << rl.from_index) - 1);
    }
    const int ph = int(e.phase);
    if (st == rail::Status::Withheld) {  // a sales policy: allowed, nothing changed
      ++withheld[ph];
      if (e.phase == Phase::Main) {
        for (uint32_t j = 0; j < b.leg_count; ++j) quality.unmet.insert({legs[j].td, int(b.seat_class)});
      }
      return;
    }
    if (st == rail::Status::SoldOut) {
      ++sold_out[ph];
      SoldOut so{e.start, e.start + e.dur, b.seat_class, b.passenger_count, b.leg_count, {}, &e};
      std::copy(legs, legs + b.leg_count, so.legs);
      sold_outs_.push_back(so);
      return;
    }
    if (st != rail::Status::Ok) {
      err_.add("book", where(e) + ": unknown status " + std::to_string(e.status));
      return;
    }
    if (e.seats == kNoSeats || e.seats >= log_.seat_blocks.size()) {
      err_.add("log", where(e) + ": Ok book without seats");
      return;
    }
    if (e.value == 0 || !order_index_.emplace(e.value, uint32_t(orders_.size())).second) {
      err_.add("book", where(e) + ": order id " + std::to_string(e.value) + " is zero or reused");
      return;
    }
    ++books_ok[ph];
    if (e.phase == Phase::Main && in && in->kind == IntentKind::Purchase) purchased_.insert(e.intent);
    Order o{};
    o.phase = e.phase;
    o.book_start = e.start;
    o.book_end = e.start + e.dur;
    o.leg_count = b.leg_count;
    const SeatBlock& sb = log_.seat_blocks[e.seats];
    const uint32_t order = uint32_t(orders_.size());
    const int n = static_cast<int>(b.passenger_count);
    for (uint32_t j = 0; j < b.leg_count; ++j) {
      o.legs[j] = legs[j];
      int idx[rail::kMaxPassengers];
      bool leg_ok = true;
      for (int p = 0; p < n; ++p) {
        idx[p] = seat_index(b.seat_class, sb.seats[j][p]);
        if (idx[p] < 0) {
          err_.add("seat", where(e) + ": " + seat_name(sb.seats[j][p]) + " is not a seat of the requested class");
          leg_ok = false;
        }
        for (int q = 0; q < p && leg_ok; ++q) {
          if (idx[q] == idx[p]) {
            err_.add("seat", where(e) + ": seat " + seat_name(sb.seats[j][p]) + " given to two passengers");
            leg_ok = false;
          }
        }
      }
      if (!leg_ok) continue;
      auto& seats = seats_of(legs[j].td);
      for (int p = 0; p < n; ++p) seats[idx[p]].push_back({order, legs[j].mask});
      if (in && in->kind == IntentKind::Purchase) {
        for (int p = 0; p < n; ++p) {
          if (b.window[p]) {
            ++window_wanted[ph];
            window_got[ph] += is_window(sb.seats[j][p]);
          }
        }
        if (b.together && n >= 2) {
          ++together_wanted[ph];
          together_got[ph] += seated_together(b.seat_class, sb.seats[j], n);
        }
      }
    }
    orders_.push_back(o);
  }

  void collect_refund(const Event& e) {
    const Intent* in = intent_of(e);
    if (!in) return;
    const auto st = static_cast<rail::Status>(e.status);
    if (st != rail::Status::Ok && st != rail::Status::NotFound) {
      err_.add("refund", where(e) + ": unknown status " + std::to_string(e.status));
      return;
    }
    if (e.value == 0) {
      if (st != rail::Status::NotFound) err_.add("refund", where(e) + ": refund of order 0 was not NotFound");
      return;
    }
    const auto it = order_index_.find(e.value);
    if (it == order_index_.end()) {
      if (st == rail::Status::Ok) err_.add("refund", where(e) + ": Ok refund of an order that was never issued");
      return;
    }
    refunds_[it->second].push_back(&e);
  }

  void check_refunds() {
    for (auto& [order, events] : refunds_) {
      Order& o = orders_[order];
      const Event* ok = nullptr;
      for (const Event* e : events) {
        if (static_cast<rail::Status>(e->status) != rail::Status::Ok) continue;
        if (ok) {
          err_.add("refund", where(*e) + ": order " + std::to_string(e->value) + " refunded successfully twice");
          continue;
        }
        ok = e;
      }
      if (ok) {
        o.refund_start = ok->start;
        o.refund_end = ok->start + ok->dur;
      }
      for (const Event* e : events) {
        if (static_cast<rail::Status>(e->status) != rail::Status::NotFound) continue;
        const uint64_t s = e->start, end = e->start + e->dur;
        // Certainly live during the call: booked before it, not refunded before it ended.
        if (o.book_end < s && o.refund_start >= end) {
          err_.add("refund", where(*e) + ": NotFound for live order " + std::to_string(e->value));
        }
      }
    }
  }

  // Every intent that issues a query must have exactly one query event.
  void check_coverage() {
    for (int ph = 0; ph < 2; ++ph) {
      uint64_t expected = 0;
      for (int64_t a : answer_of_[ph]) expected += a >= 0;
      if (queries_seen_[ph] != expected) {
        err_.add("log", std::string(ph ? "quiet" : "main") + " phase has " + std::to_string(queries_seen_[ph]) +
                            " queries, expected " + std::to_string(expected) + " (run did not complete?)");
      }
    }
  }

  // Could `a` have been released before `b` was taken?
  bool released_before(const Order& a, const Order& b) const { return a.refund_start < b.book_end; }

  void check_oversell() {
    for (size_t td = 0; td < seats_.size(); ++td) {
      for (size_t seat = 0; seat < seats_[td].size(); ++seat) {
        const auto& holds = seats_[td][seat];
        for (size_t i = 0; i < holds.size(); ++i) {
          for (size_t j = i + 1; j < holds.size(); ++j) {
            if (!(holds[i].mask & holds[j].mask)) continue;
            const Order& a = orders_[holds[i].order];
            const Order& b = orders_[holds[j].order];
            if (released_before(a, b) || released_before(b, a)) continue;
            err_.add("oversell", "train " + w_.trains[td / rail::kDays].name + " day " +
                                     std::to_string(td % rail::kDays) + " seat index " + std::to_string(seat) +
                                     " sold to two live orders over overlapping intervals");
          }
        }
      }
    }
  }

  void check_false_negatives() {
    for (const SoldOut& so : sold_outs_) {
      const ClassInfo& ci = class_info(so.cls);
      bool all_enough = true;
      for (uint32_t j = 0; j < so.leg_count; ++j) {
        const auto* seats = seats_[so.legs[j].td].empty() ? nullptr : &seats_[so.legs[j].td];
        uint32_t free = 0;
        for (int k = ci.first_seat; k < ci.first_seat + ci.seats(); ++k) {
          bool maybe_taken = false;
          if (seats) {
            for (const Hold& h : (*seats)[k]) {
              if (!(h.mask & so.legs[j].mask)) continue;
              const Order& o = orders_[h.order];
              // The hold may have been live at some moment of [start, end].
              if (o.book_start < so.end && o.refund_end > so.start) {
                maybe_taken = true;
                break;
              }
            }
          }
          free += !maybe_taken;
          if (free >= so.passengers) break;
        }
        // A leg that may have been short is where the demand went unmet.
        if (free < so.passengers && so.ev->phase == Phase::Main) quality.unmet.insert({so.legs[j].td, int(so.cls)});
        all_enough = all_enough && free >= so.passengers;
      }
      if (all_enough) {
        err_.add("false_negative", where(*so.ev) + ": SoldOut although every leg certainly had enough free seats");
      }
    }
  }

  // Satisfaction of main-phase purchases; utilization of the (train-day,
  // class) pairs that turned someone down, from the main-phase orders still
  // live when the main phase ended.
  void measure_quality() {
    for (size_t i = 0; i < load_.intents.size(); ++i) {
      if (load_.intents[i].kind != IntentKind::Purchase || answer_of_[0][i] < 0) continue;
      quality.purchases += !answers_[answer_of_[0][i]].items.empty();
    }
    quality.purchases_ok = purchased_.size();
    quality.sold_out = sold_out[int(Phase::Main)];
    quality.withheld = withheld[int(Phase::Main)];
    quality.measure(w_, [&](uint32_t td, int seat) {
      uint64_t mask = 0;
      if (seats_[td].empty()) return mask;
      for (const Hold& h : seats_[td][seat]) {
        const Order& o = orders_[h.order];
        if (o.phase == Phase::Main && o.refund_start == kNever) mask |= h.mask;
      }
      return mask;
    });
  }

  std::vector<std::vector<Hold>>& seats_of(uint32_t td) {
    auto& v = seats_[td];
    if (v.empty()) v.resize(rail::kSeatsPerTrainDay);
    return v;
  }

  const World& w_;
  const Workload& load_;
  std::vector<Answer> answers_;
  const BenchLog& log_;
  Errors& err_;

  std::vector<int64_t> answer_of_[2];
  std::unordered_set<uint32_t> purchased_;  // main-phase purchase intents that got a ticket
  uint64_t queries_seen_[2] = {};
  std::vector<Order> orders_;
  std::unordered_map<uint64_t, uint32_t> order_index_;
  std::unordered_map<uint32_t, std::vector<const Event*>> refunds_;
  std::vector<std::vector<std::vector<Hold>>> seats_;  // [td][seat] -> holds
  std::vector<SoldOut> sold_outs_;
};

std::string json_escape(const std::string& s) {
  std::string o;
  for (char c : s) {
    if (c == '"' || c == '\\') o += '\\';
    if (static_cast<unsigned char>(c) < 0x20) continue;
    o += c;
  }
  return o;
}

std::string pct_json(const Percentiles& p) {
  char buf[160];
  std::snprintf(buf, sizeof buf, "{\"count\": %llu, \"p50_ns\": %llu, \"p99_ns\": %llu, \"max_ns\": %llu}",
                (unsigned long long)p.count, (unsigned long long)p.p50, (unsigned long long)p.p99,
                (unsigned long long)p.max);
  return buf;
}

double ratio(uint64_t a, uint64_t b) { return b ? double(a) / double(b) : 1.0; }

// Latency histogram with 4 linear sub-buckets per power of two: bucket
// 4*m + s holds durations in [2^m * (1 + s/4), 2^m * (1 + (s+1)/4)).
constexpr int kBucketsPerOctave = 4;
constexpr int kHistBuckets = 32 * kBucketsPerOctave;
constexpr int kTimelineBins = 50;

int hist_bucket(uint32_t d) {
  if (d == 0) return 0;
  const int m = 31 - __builtin_clz(d);
  const int sub = static_cast<int>((uint64_t(d - (1u << m)) * kBucketsPerOctave) >> m);
  return m * kBucketsPerOctave + sub;
}

std::string counts_json(const std::vector<uint64_t>& v) {
  size_t n = v.size();
  while (n > 0 && v[n - 1] == 0) --n;
  std::string o = "[";
  for (size_t i = 0; i < n; ++i) o += (i ? "," : "") + std::to_string(v[i]);
  return o + "]";
}

int run(int argc, char** argv) {
  std::vector<std::string> pos;
  std::string json_path;
  size_t max_errors = 20;
  for (int i = 1; i < argc; ++i) {
    if (std::strcmp(argv[i], "--json") == 0 && i + 1 < argc) {
      json_path = argv[++i];
    } else if (std::strcmp(argv[i], "--max-errors") == 0 && i + 1 < argc) {
      max_errors = std::stoul(argv[++i]);
    } else {
      pos.push_back(argv[i]);
    }
  }
  if (pos.size() != 4) {
    std::fprintf(stderr,
                 "usage: railjudge <world> <load> <reference.answers> <bench.log> [--json OUT] [--max-errors N]\n");
    return 2;
  }
  const World world = load_world(pos[0]);
  validate_world(world);
  const Workload load = load_workload(pos[1]);
  std::vector<Answer> answers;
  {
    std::ifstream in(pos[2], std::ios::binary);
    if (!in) throw std::runtime_error("cannot read " + pos[2]);
    std::string line;
    while (std::getline(in, line)) {
      Answer a;
      if (!parse_answer(line, &a)) throw std::runtime_error("malformed reference answer line " + std::to_string(answers.size()));
      answers.push_back(std::move(a));
    }
  }
  const BenchLog log = read_bench_log(pos[3]);

  Errors errors;
  errors.max_kept = max_errors;
  Judge judge(world, load, std::move(answers), log, errors);
  judge.run();

  // Performance: main phase only.
  std::vector<uint32_t> lat_all, lat_op[3];
  std::vector<uint64_t> hist[3], timeline(kTimelineBins, 0);
  for (auto& h : hist) h.assign(kHistBuckets, 0);
  for (const Event& e : log.events) {
    if (e.phase != Phase::Main) continue;
    lat_all.push_back(e.dur);
    lat_op[int(e.op) % 3].push_back(e.dur);
    ++hist[int(e.op) % 3][hist_bucket(e.dur)];
    if (log.main_ns) {
      const uint64_t bin = (e.start + e.dur) * kTimelineBins / log.main_ns;
      ++timeline[std::min<uint64_t>(bin, kTimelineBins - 1)];
    }
  }
  const Percentiles all = percentiles(lat_all), q = percentiles(lat_op[0]), b = percentiles(lat_op[1]),
                    r = percentiles(lat_op[2]);
  const double throughput = log.main_ns ? double(all.count) * 1e9 / double(log.main_ns) : 0.0;

  std::string j = "{\n";
  j += "  \"ok\": " + std::string(errors.any() ? "false" : "true") + ",\n";
  j += "  \"threads\": " + std::to_string(log.threads) + ",\n";
  char buf[512];
  std::snprintf(buf, sizeof buf, "  \"init_s\": %.6f,\n  \"main_s\": %.6f,\n  \"quiet_s\": %.6f,\n  \"throughput\": %.1f,\n",
                double(log.init_ns) / 1e9, double(log.main_ns) / 1e9, double(log.quiet_ns) / 1e9, throughput);
  j += buf;
  j += "  \"latency\": {\"all\": " + pct_json(all) + ",\n              \"query\": " + pct_json(q) +
       ",\n              \"book\": " + pct_json(b) + ",\n              \"refund\": " + pct_json(r) + "},\n";
  j += "  \"histogram\": {\"buckets_per_octave\": " + std::to_string(kBucketsPerOctave) +
       ",\n                \"query\": " + counts_json(hist[0]) + ",\n                \"book\": " + counts_json(hist[1]) +
       ",\n                \"refund\": " + counts_json(hist[2]) + "},\n";
  j += "  \"timeline\": {\"bins\": " + std::to_string(kTimelineBins) + ", \"completed\": " + counts_json(timeline) + "},\n";
  for (int ph = 0; ph < 2; ++ph) {
    std::snprintf(buf, sizeof buf,
                  "  \"%s\": {\"books_ok\": %llu, \"sold_out\": %llu, \"withheld\": %llu, \"window_rate\": %.4f, "
                  "\"window_wanted\": %llu, \"together_rate\": %.4f, \"together_wanted\": %llu},\n",
                  ph ? "quiet" : "main", (unsigned long long)judge.books_ok[ph], (unsigned long long)judge.sold_out[ph],
                  (unsigned long long)judge.withheld[ph],
                  ratio(judge.window_got[ph], judge.window_wanted[ph]), (unsigned long long)judge.window_wanted[ph],
                  ratio(judge.together_got[ph], judge.together_wanted[ph]),
                  (unsigned long long)judge.together_wanted[ph]);
    j += buf;
  }
  {
    const Quality& qu = judge.quality;
    char util[32] = "null";
    if (qu.utilization() >= 0) std::snprintf(util, sizeof util, "%.6f", qu.utilization());
    std::snprintf(buf, sizeof buf,
                  "  \"quality\": {\"purchases\": %llu, \"purchases_ok\": %llu, \"satisfaction\": %.6f, "
                  "\"unmet_train_classes\": %zu, \"sold_seat_minutes\": %lld, \"capacity_seat_minutes\": %lld, "
                  "\"utilization\": %s},\n",
                  (unsigned long long)qu.purchases, (unsigned long long)qu.purchases_ok, qu.satisfaction(),
                  qu.unmet.size(), (long long)qu.sold_minutes, (long long)qu.capacity_minutes, util);
    j += buf;
  }
  j += "  \"error_counts\": {";
  bool first = true;
  for (const auto& [kind, n] : errors.by_kind) {
    j += (first ? "" : ", ") + std::string("\"") + kind + "\": " + std::to_string(n);
    first = false;
  }
  j += "},\n  \"errors\": [";
  for (size_t i = 0; i < errors.kept.size(); ++i) j += (i ? ",\n             \"" : "\"") + json_escape(errors.kept[i]) + "\"";
  j += "]\n}\n";

  if (json_path.empty()) {
    std::fputs(j.c_str(), stdout);
  } else {
    std::ofstream out(json_path, std::ios::binary);
    out << j;
    if (!out) throw std::runtime_error("cannot write " + json_path);
  }
  std::fprintf(stderr, "railjudge: %s, %.0f calls/s, p99 %.1f us%s\n", errors.any() ? "FAILED" : "ok", throughput,
               double(all.p99) / 1e3, errors.any() ? "" : "");
  for (const auto& e : errors.kept) std::fprintf(stderr, "  %s\n", e.c_str());
  return errors.any() ? 1 : 0;
}

}  // namespace

int main(int argc, char** argv) {
  try {
    return run(argc, argv);
  } catch (const std::exception& e) {
    std::fprintf(stderr, "railjudge: %s\n", e.what());
    return 2;
  }
}
