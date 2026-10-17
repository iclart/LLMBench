// Request validity rules from TASK.md sections 4.1, 4.5 and 5.1, shared by
// the generator, the referee and the reference implementations' tests.
#pragma once

#include <cstdint>

#include "ticket_system.h"
#include "world.h"

namespace railtools {

struct ResolvedLeg {
  uint32_t from_index = 0;  // stop index of the boarding station
  uint32_t to_index = 0;    // stop index of the alighting station
  int32_t depart = 0;       // absolute minute
  int32_t arrive = 0;       // absolute minute
};

// Checks rule 4.1.1 for one leg and fills `out`.
bool resolve_leg(const World& world, const rail::LegRef& leg, ResolvedLeg* out);

// Rules 4.1.1 - 4.1.4. Requires 1 <= n <= kMaxLegs.
bool is_valid_itinerary(const World& world, const rail::LegRef* legs, uint32_t n);

// Rule 4.5.
bool is_valid_query(const World& world, const rail::QueryRequest& req);

// Rule 5.1.
bool is_valid_book(const World& world, const rail::BookRequest& req);

}  // namespace railtools
