// Functional test suite (TASK.md 9.3): small single-threaded cases, each
// testing one rule, grouped by category. railrun func runs a suite against an
// engine and reports the pass rate per category; the functional score is the
// mean of those rates.
//
// File format (ASCII, single spaces):
//
//   RAILSUITE 1
//   CASES <n>
//   C <category>                                   starts a case
//   Q <from> <to> <earliest> <k> | <answer>        query; <answer> as in answers.h
//   B <declared> <listed> (<train> <day> <from> <to>) * listed
//     <class> <pax> <window_mask> <together> [@<label>]
//                                                  book; checked against the rules
//   R @<label> | R #<order id>                     refund
//   E                                              ends the case
//   END
//
// Books are not compared with a reference (which seats an engine picks
// changes what later books can get) but checked like railrun verify: Invalid
// exactly for malformed requests, valid distinct seats of the class, no
// overlap with live tickets, SoldOut only when some leg is really short;
// Withheld is always allowed. A refund of a label is expected to succeed while
// that order is live and to return NotFound otherwise; a label whose book did
// not succeed is skipped. Cases use disjoint train-days, so a failed case
// does not affect the next one.
#pragma once

#include <cstdint>
#include <iosfwd>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "ticket_system.h"
#include "world.h"

namespace railtools {

struct SuiteOp {
  char kind = 0;                // 'Q', 'B' or 'R'
  rail::QueryRequest query{};   // Q
  std::string answer;           // Q: expected answer line
  rail::BookRequest book{};     // B
  uint32_t listed = 0;          // B: legs listed (may differ from book.leg_count when malformed)
  std::string label;            // B: optional; R: the order to refund, if by label
  uint64_t order_id = 0;        // R: by id, if no label
};

struct SuiteCase {
  std::string category;
  std::vector<SuiteOp> ops;
};

std::vector<SuiteCase> load_suite(const std::string& path);
void write_suite(std::ostream& out, const std::vector<SuiteCase>& cases);

class SuiteRunner {
 public:
  SuiteRunner(const World& world, rail::TicketSystem& sys) : w_(world), sys_(sys) {}

  // Runs one case. Returns an empty string if it passed, else what failed.
  // With record, query answers are not checked but stored into the case.
  std::string run(SuiteCase& c, bool record);

 private:
  struct Hold {
    uint64_t td;
    int seat;
    uint64_t mask;
  };
  struct Order {
    uint64_t id = 0;
    bool live = false;
    std::vector<Hold> holds;
  };

  std::string query(SuiteOp& op, bool record);
  std::string book(const SuiteOp& op, std::unordered_map<std::string, Order>& orders);
  std::string refund(const SuiteOp& op, std::unordered_map<std::string, Order>& orders);
  std::vector<uint64_t>& seats_of(uint64_t td);

  const World& w_;
  rail::TicketSystem& sys_;
  std::unordered_map<uint64_t, std::vector<uint64_t>> occ_;  // train-day -> seat -> occupied stop intervals
  std::unordered_set<uint64_t> issued_;
};

}  // namespace railtools
