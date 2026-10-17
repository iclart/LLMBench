// Workload = a scenario's script of user intents, played by the harness.
//
// The concurrent phase is split round-robin across the harness threads
// (intent i goes to thread i % T). The quiet phase is replayed afterwards on a
// single thread; its results are deterministic and are used for the
// false-negative cross-check and the seat-preference score.
//
// File format (ASCII, one intent per line, single spaces):
//
//   RAILLOAD 1
//   SCENARIO <name>
//   INTENTS <n>
//   <intent>                                  n lines
//   QUIET <m>
//   <intent>                                  m lines, purchases only
//   END
//
// Intents:
//   P  <from> <to> <earliest> <k> <class> <pax> <window_mask> <together>
//        query, then try to book the returned itineraries in order until one
//        succeeds; a successful order joins the global pool of live orders
//   Q  <from> <to> <earliest> <k>             query only
//   R                                         refund a random live order
//   RD                                        refund a random already-refunded order
//   XQ <from> <to> <earliest> <k>             malformed query
//   XB <declared> <listed> (<train> <day> <from> <to>) * listed
//      <class> <pax> <window_mask> <together> malformed book
//   XR                                        refund order id 0
#pragma once

#include <cstdint>
#include <iosfwd>
#include <string>
#include <vector>

#include "ticket_system.h"

namespace railtools {

enum class IntentKind : uint8_t {
  Purchase,
  Browse,
  Refund,
  DupRefund,
  BadQuery,
  BadBook,
  BadRefund,
};

struct Intent {
  IntentKind kind = IntentKind::Browse;
  uint8_t seat_class = 0;      // Purchase
  uint8_t passengers = 0;      // Purchase
  uint8_t window_mask = 0;     // Purchase; bit p = passenger p wants a window seat
  bool together = false;       // Purchase
  rail::QueryRequest query{};  // Purchase, Browse, BadQuery
  uint32_t bad_book = 0;       // BadBook: index into Workload::bad_books
};

struct Workload {
  std::string scenario;
  std::vector<Intent> intents;
  std::vector<Intent> quiet;
  std::vector<rail::BookRequest> bad_books;
};

void write_workload(std::ostream& out, const Workload& load);
Workload read_workload(std::istream& in);
Workload load_workload(const std::string& path);
void save_workload(const std::string& path, const Workload& load);

}  // namespace railtools
