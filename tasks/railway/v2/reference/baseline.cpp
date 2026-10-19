#include <memory>
#include <mutex>
#include <unordered_map>
#include "problem.h"

namespace rail::v2 {
namespace {
using namespace tools;
class Baseline final : public TicketSystem {
  std::unique_ptr<Problem> problem_;
  std::mutex mutex_;
  std::unordered_map<uint32_t, Block> inventory_;
  std::unordered_map<uint64_t, Order> orders_;
  uint64_t next_ = 1;
  Options options_{};
  static const Block& block(const std::unordered_map<uint32_t, Block>& inv, uint32_t td) {
    static const Block empty{};
    auto pos = inv.find(td); return pos == inv.end() ? empty : pos->second;
  }
  void release(std::unordered_map<uint32_t, Block>& inv, const Order& o, CellMask mask) {
    for (uint32_t p = 0; p < o.request.passenger_count; ++p)
      for (uint32_t l = 0; l < o.request.leg_count; ++l) if (mask & cell(p, l))
        inv[o.legs[l].td][seat_index(int(o.request.seat_class), o.result.seats[l][p])] &= ~o.legs[l].mask;
  }
  Status transact(const BookRequest& b, const std::array<Resolved, kMaxLegs>& legs, BookResult* out,
                  Order* old = nullptr, CellMask mask = 0) {
    // Only affected blocks are staged; failure cannot leak seats.
    std::unordered_map<uint32_t, Block> staged;
    for (uint32_t l = 0; l < b.leg_count; ++l) staged[legs[l].td] = block(inventory_, legs[l].td);
    if (old) {
      for (uint32_t l = 0; l < old->request.leg_count; ++l) staged[old->legs[l].td] = block(inventory_, old->legs[l].td);
      release(staged, *old, mask);
    }
    BookResult result{}; int cls = int(b.seat_class);
    for (uint32_t l = 0; l < b.leg_count; ++l) {
      auto& seats = staged[legs[l].td]; uint32_t p = 0;
      for (int s = first[cls]; s < first[cls] + counts[cls] && p < b.passenger_count; ++s)
        if (!(seats[s] & legs[l].mask)) { seats[s] |= legs[l].mask; result.seats[l][p++] = seat_of(cls, s); }
      if (p != b.passenger_count) return Status::SoldOut;
    }
    // No throwing insertion after publication. Rehash can invalidate old pointers.
    const uint64_t old_id = old ? old->result.order_id : 0;
    result.order_id = next_;
    orders_.emplace(next_, Order{b, result, all_cells(b.passenger_count, b.leg_count), legs});
    ++next_;
    for (auto& [td, seats] : staged) inventory_[td] = seats;
    if (old_id) orders_.at(old_id).active &= ~mask;
    *out = result; return Status::Ok;
  }
 public:
  void init(const WorldData& w, const Options& options) override { problem_ = std::make_unique<Problem>(w); options_ = options; }
  Status query(const QueryRequest& q, QueryResult* out) override {
    if (!problem_->valid_query(q)) return Status::Invalid;
    std::lock_guard<std::mutex> guard(mutex_);
    *out = exhaustive_query(*problem_, q, [&](const Resolved& r) { return Problem::available(block(inventory_, r.td), r.mask, q.seat_class, q.passenger_count); });
    return Status::Ok;
  }
  Status book(const BookRequest& b, BookResult* out) override {
    std::array<Resolved, kMaxLegs> legs{};
    if (!problem_->book(b, legs)) return Status::Invalid;
    std::lock_guard<std::mutex> guard(mutex_); return transact(b, legs, out);
  }
  Status refund(const RefundRequest& r) override {
    if (!mask_shape(r.cells)) return Status::Invalid;
    std::lock_guard<std::mutex> guard(mutex_);
    auto pos = orders_.find(r.order_id); if (pos == orders_.end()) return Status::NotFound;
    Status st = selection(pos->second, r.cells); if (st != Status::Ok) return st;
    release(inventory_, pos->second, r.cells); pos->second.active &= ~r.cells; return Status::Ok;
  }
  Status exchange(const ExchangeRequest& r, BookResult* out) override {
    std::array<Resolved, kMaxLegs> legs{};
    if (!mask_shape(r.cells) || selected_passengers(r.cells) != r.replacement.passenger_count || !problem_->book(r.replacement, legs)) return Status::Invalid;
    std::lock_guard<std::mutex> guard(mutex_);
    auto pos = orders_.find(r.order_id); if (pos == orders_.end()) return Status::NotFound;
    Status st = selection(pos->second, r.cells); if (st != Status::Ok) return st;
    return transact(r.replacement, legs, out, &pos->second, r.cells);
  }
};
}  // namespace
TicketSystem* create_ticket_system() { return new Baseline(); }
}  // namespace rail::v2
