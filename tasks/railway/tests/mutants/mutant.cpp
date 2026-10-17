// Deliberately broken engines for testing railjudge: each wraps the public
// baseline and injects one bug, selected by RAIL_MUTANT. Every mutant must
// be caught; see tests/CMakeLists.txt for the expected verdicts.
#define create_ticket_system create_baseline_ticket_system
#include "../../reference/baseline/baseline.cpp"
#undef create_ticket_system

#include <atomic>
#include <mutex>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#define MUTANT_STEAL_SEATS 1     // oversell: frees another live order's seats to sell them again
#define MUTANT_FAKE_SOLD_OUT 2   // false negative: every 7th book claims SoldOut without trying
#define MUTANT_PARTIAL_BOOK 3    // non-atomic: books legs one by one and never rolls back
#define MUTANT_DATA_RACE 4       // data race: unsynchronised call counter (TSan only)
#define MUTANT_DOUBLE_REFUND 5   // refund: a second refund of the same order also succeeds
#define MUTANT_WRONG_QUERY 6     // query: swaps the first two itineraries
#define MUTANT_BAD_SEAT 7        // seat: first-class passengers sometimes get seat 'B'
#define MUTANT_BUSY_SOLD_OUT 8   // false negative under contention: SoldOut when another call is running
#define MUTANT_LEAKY_REFUND 9    // refund of a multi-leg order releases only its first leg

#define MUTANT_LEG_BY_LEG (RAIL_MUTANT == MUTANT_PARTIAL_BOOK || RAIL_MUTANT == MUTANT_LEAKY_REFUND)

#ifndef RAIL_MUTANT
#error "define RAIL_MUTANT"
#endif

namespace rail {

TicketSystem* create_ticket_system();

namespace {

#if RAIL_MUTANT == MUTANT_DATA_RACE
uint64_t racy_calls = 0;  // written by every thread without synchronisation
#endif

class Mutant final : public TicketSystem {
 public:
  Mutant() : inner_(create_baseline_ticket_system()) {}

  void init(const WorldData& world) override {
    world_ = &world;
    inner_->init(world);
  }

  Status query(const QueryRequest& req, QueryResult* out) override {
    Busy busy(*this);
    const Status st = inner_->query(req, out);
#if RAIL_MUTANT == MUTANT_WRONG_QUERY
    if (st == Status::Ok && out->count >= 2) std::swap(out->items[0], out->items[1]);
#endif
    return st;
  }

  Status book(const BookRequest& req, BookResult* out) override {
    Busy busy(*this);
#if RAIL_MUTANT == MUTANT_FAKE_SOLD_OUT
    if (calls_.fetch_add(1) % 7 == 6) return Status::SoldOut;
#elif RAIL_MUTANT == MUTANT_BUSY_SOLD_OUT
    if (busy.others > 0) return Status::SoldOut;
#elif MUTANT_LEG_BY_LEG
    if (req.leg_count >= 2 && req.leg_count <= uint32_t(kMaxLegs)) return book_leg_by_leg(req, out);
#endif
    Status st = inner_->book(req, out);
#if RAIL_MUTANT == MUTANT_STEAL_SEATS
    if (st == Status::SoldOut) {
      uint64_t victim = 0;
      {
        std::lock_guard<std::mutex> g(mu_);
        if (!live_.empty()) victim = live_.back();
      }
      if (victim && inner_->refund(victim) == Status::Ok) st = inner_->book(req, out);
    }
    if (st == Status::Ok) {
      std::lock_guard<std::mutex> g(mu_);
      live_.push_back(out->order_id);
    }
#elif RAIL_MUTANT == MUTANT_BAD_SEAT
    if (st == Status::Ok && req.seat_class == SeatClass::First && out->order_id % 5 == 0) out->seats[0][0].letter = 'B';
#elif RAIL_MUTANT == MUTANT_DOUBLE_REFUND
    if (st == Status::Ok) {
      std::lock_guard<std::mutex> g(mu_);
      issued_.insert(out->order_id);
    }
#endif
    return st;
  }

  Status refund(uint64_t order_id) override {
    Busy busy(*this);
#if MUTANT_LEG_BY_LEG
    {
      std::unique_lock<std::mutex> g(mu_);
      const auto it = composite_.find(order_id);
      if (it != composite_.end()) {
        const std::vector<uint64_t> parts = it->second;
        composite_.erase(it);
        g.unlock();
#if RAIL_MUTANT == MUTANT_LEAKY_REFUND
        inner_->refund(parts[0]);  // the other legs stay occupied
#else
        for (uint64_t p : parts) inner_->refund(p);
#endif
        return Status::Ok;
      }
    }
#endif
    const Status st = inner_->refund(order_id);
#if RAIL_MUTANT == MUTANT_DOUBLE_REFUND
    if (st == Status::NotFound) {
      std::lock_guard<std::mutex> g(mu_);
      if (issued_.count(order_id)) return Status::Ok;
    }
#endif
    return st;
  }

 private:
  // Counts calls in progress; `others` is how many were running on entry.
  struct Busy {
    explicit Busy(Mutant& m) : m_(m), others(m.in_flight_.fetch_add(1)) {
#if RAIL_MUTANT == MUTANT_DATA_RACE
      ++racy_calls;
#endif
    }
    ~Busy() { m_.in_flight_.fetch_sub(1); }
    Mutant& m_;
    int others;
  };

#if MUTANT_LEG_BY_LEG
  // Absolute times of a leg, or false if the leg itself is malformed.
  bool times(const LegRef& l, int32_t* dep, int32_t* arr) const {
    if (l.train >= world_->train_count || l.day >= uint32_t(kDays)) return false;
    const Train& t = world_->trains[l.train];
    int a = -1, b = -1;
    for (uint32_t i = 0; i < t.stop_count; ++i) {
      if (world_->stops[t.stop_begin + i].station == l.from) a = int(i);
      if (world_->stops[t.stop_begin + i].station == l.to) b = int(i);
    }
    if (a < 0 || b < 0 || a >= b) return false;
    *dep = int32_t(l.day) * kMinutesPerDay + world_->stops[t.stop_begin + a].depart;
    *arr = int32_t(l.day) * kMinutesPerDay + world_->stops[t.stop_begin + b].arrive;
    return true;
  }

  // The whole itinerary is valid (TASK.md 4.1), so only the missing rollback is wrong.
  bool itinerary_ok(const BookRequest& req) const {
    int32_t dep[kMaxLegs], arr[kMaxLegs];
    uint32_t stations[kMaxLegs + 1] = {req.legs[0].from};
    for (uint32_t i = 0; i < req.leg_count; ++i) {
      if (!times(req.legs[i], &dep[i], &arr[i])) return false;
      stations[i + 1] = req.legs[i].to;
      for (uint32_t j = 0; j < i; ++j)
        if (req.legs[i].train == req.legs[j].train && req.legs[i].day == req.legs[j].day) return false;
    }
    for (uint32_t i = 0; i + 1 < req.leg_count; ++i) {
      if (req.legs[i].to != req.legs[i + 1].from || dep[i + 1] < arr[i] + kMinTransferMinutes) return false;
    }
    for (uint32_t i = 0; i <= req.leg_count; ++i)
      for (uint32_t j = i + 1; j <= req.leg_count; ++j)
        if (stations[i] == stations[j]) return false;
    return true;
  }

  Status book_leg_by_leg(const BookRequest& req, BookResult* out) {
    if (!itinerary_ok(req)) return inner_->book(req, out);  // let the inner engine reject it
    std::vector<uint64_t> parts;
    for (uint32_t i = 0; i < req.leg_count; ++i) {
      BookRequest one = req;
      one.leg_count = 1;
      one.legs[0] = req.legs[i];
      BookResult r{};
      const Status st = inner_->book(one, &r);
      if (st != Status::Ok) {
#if RAIL_MUTANT == MUTANT_LEAKY_REFUND
        for (uint64_t p : parts) inner_->refund(p);  // this mutant books atomically
#endif
        return st;  // MUTANT_PARTIAL_BOOK: earlier legs stay booked
      }
      parts.push_back(r.order_id);
      for (int p = 0; p < kMaxPassengers; ++p) out->seats[i][p] = r.seats[0][p];
    }
    out->order_id = (uint64_t(1) << 62) + next_composite_.fetch_add(1);
    std::lock_guard<std::mutex> g(mu_);
    composite_[out->order_id] = parts;
    return Status::Ok;
  }
  std::unordered_map<uint64_t, std::vector<uint64_t>> composite_;
  std::atomic<uint64_t> next_composite_{1};
#endif

  const WorldData* world_ = nullptr;
  TicketSystem* inner_;
  std::atomic<int> in_flight_{0};
  std::atomic<uint64_t> calls_{0};
  std::mutex mu_;
  std::vector<uint64_t> live_;
  std::unordered_set<uint64_t> issued_;
};

}  // namespace

TicketSystem* create_ticket_system() { return new Mutant(); }

}  // namespace rail
