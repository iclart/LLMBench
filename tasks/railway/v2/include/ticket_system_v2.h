#pragma once
#include "ticket_system.h"

// Experimental ABI 2. ABI 1 and its season/scoring artifacts remain unchanged.
namespace rail::v2 {
inline constexpr uint32_t kAbiVersion = 2;
enum class Mode : uint8_t { Performance, Management };
struct Options {
  Mode mode = Mode::Performance;
  uint32_t service_weight_minutes = 0;
};
struct QueryRequest {
  uint32_t from, to;
  int32_t earliest;
  uint32_t k;
  SeatClass seat_class;
  uint32_t passenger_count;
};
using CellMask = uint16_t;
// bit (passenger * kMaxLegs + leg); slots are never renumbered in an order.
constexpr CellMask cell(uint32_t passenger, uint32_t leg) {
  return passenger < kMaxPassengers && leg < kMaxLegs ? CellMask(1u << (passenger * kMaxLegs + leg)) : 0;
}
constexpr CellMask all_cells(uint32_t passengers, uint32_t legs) {
  CellMask mask = 0;
  for (uint32_t p = 0; p < passengers && p < kMaxPassengers; ++p)
    for (uint32_t l = 0; l < legs && l < kMaxLegs; ++l) mask |= cell(p, l);
  return mask;
}
struct RefundRequest { uint64_t order_id; CellMask cells; };
struct ExchangeRequest {
  uint64_t order_id;
  CellMask cells;
  BookRequest replacement;
};
class TicketSystem {
 public:
  virtual ~TicketSystem() = default;
  virtual void init(const WorldData&, const Options&) = 0;
  virtual Status query(const QueryRequest&, QueryResult*) = 0;
  virtual Status book(const BookRequest&, BookResult*) = 0;
  virtual Status refund(const RefundRequest&) = 0;
  virtual Status exchange(const ExchangeRequest&, BookResult*) = 0;
};
TicketSystem* create_ticket_system();
}  // namespace rail::v2
