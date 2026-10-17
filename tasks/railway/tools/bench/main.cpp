// railbench - concurrent closed-loop harness linked against one engine.
//
//   railbench <world> <load> <threads> <out.log> [--seed S]
//
// Intent i of the main phase goes to thread i % threads; every thread plays
// its intents back to back. Each engine call is timed and logged (see
// benchlog.h). Afterwards the quiet phase is replayed on a single thread,
// followed by the audit: every leg of a failed multi-leg booking and of a
// refunded order is booked again and again until SoldOut, so that seats an
// engine failed to give back become visible. railbench checks nothing
// itself: railjudge reads the log.
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <tuple>
#include <unordered_map>
#include <vector>

#include "answers.h"
#include "benchlog.h"
#include "rng.h"
#include "ticket_system.h"
#include "workload.h"
#include "world.h"

namespace {

using namespace railtools;
using Clock = std::chrono::steady_clock;

// Append-only storage that never moves elements and allocates rarely, so
// logging stays cheap and does not disturb the measurement.
template <class T>
class Chunked {
 public:
  static constexpr size_t kChunk = 1 << 15;

  T& push() {
    if (size_ % kChunk == 0) chunks_.push_back(std::make_unique<T[]>(kChunk));
    T& slot = chunks_[size_ / kChunk][size_ % kChunk];
    ++size_;
    return slot;
  }
  size_t size() const { return size_; }
  const T& operator[](size_t i) const { return chunks_[i / kChunk][i % kChunk]; }

 private:
  std::vector<std::unique_ptr<T[]>> chunks_;
  size_t size_ = 0;
};

// Order ids handed between threads for refunds. Slots are written once and
// cleared after a successful refund; readers skip empty slots.
class OrderPool {
 public:
  explicit OrderPool(size_t capacity) : slots_(std::make_unique<std::atomic<uint64_t>[]>(capacity)), cap_(capacity) {
    for (size_t i = 0; i < capacity; ++i) slots_[i].store(0, std::memory_order_relaxed);
  }

  void push(uint64_t id) {
    const size_t i = count_.fetch_add(1, std::memory_order_relaxed);
    if (i < cap_) slots_[i].store(id, std::memory_order_release);
  }

  // A random slot index, or SIZE_MAX if the pool is empty.
  size_t pick(Rng& rng) const {
    const size_t n = std::min(count_.load(std::memory_order_acquire), cap_);
    return n == 0 ? SIZE_MAX : static_cast<size_t>(rng.below(n));
  }

  uint64_t get(size_t i) const { return slots_[i].load(std::memory_order_acquire); }
  void clear(size_t i, uint64_t id) { slots_[i].compare_exchange_strong(id, 0, std::memory_order_acq_rel); }

 private:
  std::unique_ptr<std::atomic<uint64_t>[]> slots_;
  size_t cap_;
  std::atomic<size_t> count_{0};
};

struct Shared {
  rail::TicketSystem& sys;
  const Workload& load;
  Clock::time_point t0;
  OrderPool live, refunded;
};

class Player {
 public:
  Player(Shared& sh, uint64_t seed, Phase phase) : sh_(sh), rng_(seed), phase_(phase) {}

  void play(uint32_t index, const Intent& in) {
    switch (in.kind) {
      case IntentKind::Purchase: {
        rail::QueryResult r{};
        query(index, in.query, &r);
        const uint32_t tries = r.count < uint32_t(rail::kMaxQueryResults) ? r.count : uint32_t(rail::kMaxQueryResults);
        for (uint32_t a = 0; a < tries; ++a) {
          rail::BookRequest b{};
          const auto& it = r.items[a];
          b.leg_count = it.leg_count;
          for (uint32_t j = 0; j < it.leg_count && j < uint32_t(rail::kMaxLegs); ++j) {
            b.legs[j] = {it.legs[j].train, it.legs[j].day, it.legs[j].from, it.legs[j].to};
          }
          b.seat_class = static_cast<rail::SeatClass>(in.seat_class);
          b.passenger_count = in.passengers;
          for (int p = 0; p < rail::kMaxPassengers; ++p) b.window[p] = (in.window_mask >> p) & 1;
          b.together = in.together;
          if (book(index, b, static_cast<uint8_t>(a)) == rail::Status::Ok) break;
        }
        break;
      }
      case IntentKind::Browse:
      case IntentKind::BadQuery: {
        rail::QueryResult r{};
        query(index, in.query, &r);
        break;
      }
      case IntentKind::BadBook:
        book(index, sh_.load.bad_books[in.bad_book], kBadBookAttempt);
        break;
      case IntentKind::Refund: {
        const size_t slot = sh_.live.pick(rng_);
        if (slot == SIZE_MAX) break;
        const uint64_t id = sh_.live.get(slot);
        if (id == 0) break;
        if (refund(index, id) == rail::Status::Ok) {
          sh_.live.clear(slot, id);
          sh_.refunded.push(id);
        }
        break;
      }
      case IntentKind::DupRefund: {
        const size_t slot = sh_.refunded.pick(rng_);
        if (slot == SIZE_MAX) break;
        const uint64_t id = sh_.refunded.get(slot);
        if (id != 0) refund(index, id);
        break;
      }
      case IntentKind::BadRefund:
        refund(index, 0);
        break;
    }
  }

  // Books `p` until SoldOut, five passengers at a time, then one at a time.
  void audit(uint32_t index, const Probe& p) {
    for (uint32_t n : {5u, 1u}) {
      for (int i = 0; i <= rail::kSeatsPerTrainDay; ++i) {
        rail::BookRequest b{};
        b.leg_count = 1;
        b.legs[0] = p.leg;
        b.seat_class = static_cast<rail::SeatClass>(p.seat_class);
        b.passenger_count = n;
        if (book(index, b, static_cast<uint8_t>(n)) != rail::Status::Ok) break;
      }
    }
  }

  struct BookedOrder {
    uint64_t id;
    uint32_t leg_count;
    Probe legs[rail::kMaxLegs];
  };

  Chunked<Event> events;
  Chunked<SeatBlock> seats;
  // Audit candidates, recorded after the timed calls.
  std::vector<Probe> failed_legs;    // legs of multi-leg books that returned SoldOut
  std::vector<BookedOrder> booked;   // successful orders
  std::vector<uint64_t> refunded;    // successfully refunded order ids

 private:
  Event& log(uint32_t index, Op op, rail::Status st, Clock::time_point a, Clock::time_point b) {
    Event& e = events.push();
    const int64_t start = std::chrono::duration_cast<std::chrono::nanoseconds>(a - sh_.t0).count();
    const int64_t dur = std::chrono::duration_cast<std::chrono::nanoseconds>(b - a).count();
    e.start = static_cast<uint64_t>(start < 0 ? 0 : start);
    e.dur = dur > int64_t(UINT32_MAX) ? UINT32_MAX : static_cast<uint32_t>(dur);
    e.intent = index;
    e.value = 0;
    e.seats = kNoSeats;
    e.op = op;
    e.status = static_cast<uint8_t>(st);
    e.attempt = 0;
    e.phase = phase_;
    return e;
  }

  void query(uint32_t index, const rail::QueryRequest& q, rail::QueryResult* r) {
    const auto a = Clock::now();
    const rail::Status st = sh_.sys.query(q, r);
    const auto b = Clock::now();
    log(index, Op::Query, st, a, b).value = answer_hash(st, *r);
    if (st != rail::Status::Ok) r->count = 0;
  }

  rail::Status book(uint32_t index, const rail::BookRequest& req, uint8_t attempt) {
    rail::BookResult res{};
    const auto a = Clock::now();
    const rail::Status st = sh_.sys.book(req, &res);
    const auto b = Clock::now();
    Event& e = log(index, Op::Book, st, a, b);
    e.attempt = attempt;
    const uint32_t legs = std::min<uint32_t>(req.leg_count, rail::kMaxLegs);
    if (st == rail::Status::Ok) {
      e.value = res.order_id;
      e.seats = static_cast<uint32_t>(seats.size());
      std::memcpy(seats.push().seats, res.seats, sizeof res.seats);
      if (attempt != kBadBookAttempt) {
        sh_.live.push(res.order_id);
        BookedOrder o{res.order_id, legs, {}};
        for (uint32_t j = 0; j < legs; ++j) o.legs[j] = {req.legs[j], static_cast<uint32_t>(req.seat_class)};
        booked.push_back(o);
      }
    } else if (st == rail::Status::SoldOut && legs >= 2) {
      for (uint32_t j = 0; j < legs; ++j) failed_legs.push_back({req.legs[j], static_cast<uint32_t>(req.seat_class)});
    }
    return st;
  }

  rail::Status refund(uint32_t index, uint64_t id) {
    const auto a = Clock::now();
    const rail::Status st = sh_.sys.refund(id);
    const auto b = Clock::now();
    log(index, Op::Refund, st, a, b).value = id;
    if (st == rail::Status::Ok) refunded.push_back(id);
    return st;
  }

  Shared& sh_;
  Rng rng_;
  Phase phase_;
};

bool probe_less(const Probe& a, const Probe& b) {
  const auto key = [](const Probe& p) {
    return std::make_tuple(p.leg.train, p.leg.day, p.leg.from, p.leg.to, p.seat_class);
  };
  return key(a) < key(b);
}

// Distinct audit intervals from all players, at most `cap`, spread evenly.
std::vector<Probe> audit_probes(const std::vector<std::unique_ptr<Player>>& players, size_t cap) {
  std::vector<Probe> all;
  std::unordered_map<uint64_t, const Player::BookedOrder*> orders;
  for (const auto& p : players) {
    all.insert(all.end(), p->failed_legs.begin(), p->failed_legs.end());
    for (const auto& o : p->booked) orders.emplace(o.id, &o);
  }
  for (const auto& p : players) {
    for (uint64_t id : p->refunded) {
      const auto it = orders.find(id);
      if (it != orders.end()) all.insert(all.end(), it->second->legs, it->second->legs + it->second->leg_count);
    }
  }
  std::sort(all.begin(), all.end(), probe_less);
  all.erase(std::unique(all.begin(), all.end(), [](const Probe& a, const Probe& b) {
              return !probe_less(a, b) && !probe_less(b, a);
            }),
            all.end());
  if (all.size() <= cap) return all;
  std::vector<Probe> picked;
  for (size_t i = 0; i < cap; ++i) picked.push_back(all[i * all.size() / cap]);
  return picked;
}

uint64_t ns_between(Clock::time_point a, Clock::time_point b) {
  return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(b - a).count());
}

int run(int argc, char** argv) {
  std::vector<std::string> pos;
  uint64_t seed = 1;
  for (int i = 1; i < argc; ++i) {
    if (std::strcmp(argv[i], "--seed") == 0 && i + 1 < argc) {
      seed = std::stoull(argv[++i]);
    } else {
      pos.push_back(argv[i]);
    }
  }
  if (pos.size() != 4) {
    std::fprintf(stderr, "usage: railbench <world> <load> <threads> <out.log> [--seed S]\n");
    return 2;
  }
  const uint32_t threads = static_cast<uint32_t>(std::stoul(pos[2]));
  if (threads < 1 || threads > 256) throw std::runtime_error("threads must be in 1..256");

  const World world = load_world(pos[0]);
  validate_world(world);
  const Workload load = load_workload(pos[1]);
  const WorldView view(world);

  size_t purchases = 0;
  for (const auto* phase : {&load.intents, &load.quiet})
    for (const auto& in : *phase) purchases += in.kind == IntentKind::Purchase;

  // Never destroyed (see ticket_system.h); kept in a static so it stays
  // reachable and LeakSanitizer does not report the engine's memory.
  static rail::TicketSystem* sys = nullptr;
  sys = rail::create_ticket_system();
  BenchLog out;
  out.threads = threads;
  out.seed = seed;
  out.main_intents = load.intents.size();
  out.quiet_intents = load.quiet.size();

  const auto i0 = Clock::now();
  sys->init(view.view());
  out.init_ns = ns_between(i0, Clock::now());

  Shared sh{*sys, load, {}, OrderPool(purchases + 1), OrderPool(purchases + 1)};
  std::vector<std::unique_ptr<Player>> players;
  for (uint32_t t = 0; t < threads; ++t) {
    players.push_back(std::make_unique<Player>(sh, seed * 1000003 + t, Phase::Main));
  }

  // Release all threads at once, then time until the last one finishes.
  std::atomic<bool> go{false};
  std::vector<std::thread> pool;
  for (uint32_t t = 0; t < threads; ++t) {
    pool.emplace_back([&, t] {
      while (!go.load(std::memory_order_acquire)) std::this_thread::yield();
      Player& p = *players[t];
      for (size_t i = t; i < load.intents.size(); i += threads) p.play(static_cast<uint32_t>(i), load.intents[i]);
    });
  }
  sh.t0 = Clock::now();
  go.store(true, std::memory_order_release);
  for (auto& th : pool) th.join();
  const auto main_end = Clock::now();
  out.main_ns = ns_between(sh.t0, main_end);

  Player quiet(sh, seed * 1000003 + 999, Phase::Quiet);
  for (size_t i = 0; i < load.quiet.size(); ++i) quiet.play(static_cast<uint32_t>(i), load.quiet[i]);
  out.quiet_ns = ns_between(main_end, Clock::now());

  players.push_back(std::make_unique<Player>(std::move(quiet)));

  constexpr size_t kMaxProbes = 500;
  out.probes = audit_probes(players, kMaxProbes);
  const auto audit_start = Clock::now();
  auto auditor = std::make_unique<Player>(sh, seed * 1000003 + 1000, Phase::Audit);
  for (size_t i = 0; i < out.probes.size(); ++i) auditor->audit(static_cast<uint32_t>(i), out.probes[i]);
  out.audit_ns = ns_between(audit_start, Clock::now());
  players.push_back(std::move(auditor));

  for (const auto& p : players) {
    const uint32_t seat_base = static_cast<uint32_t>(out.seat_blocks.size());
    for (size_t i = 0; i < p->seats.size(); ++i) out.seat_blocks.push_back(p->seats[i]);
    for (size_t i = 0; i < p->events.size(); ++i) {
      Event e = p->events[i];
      if (e.seats != kNoSeats) e.seats += seat_base;
      out.events.push_back(e);
    }
  }
  write_bench_log(pos[3], out);
  std::fprintf(stderr, "railbench: %u threads, init %.3f s, main %.3f s, quiet %.3f s, audit %.3f s (%zu probes), %zu calls\n",
               threads, double(out.init_ns) / 1e9, double(out.main_ns) / 1e9, double(out.quiet_ns) / 1e9,
               double(out.audit_ns) / 1e9, out.probes.size(), out.events.size());
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  try {
    return run(argc, argv);
  } catch (const std::exception& e) {
    std::fprintf(stderr, "railbench: %s\n", e.what());
    return 1;
  }
}
