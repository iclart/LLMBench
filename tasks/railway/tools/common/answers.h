// Canonical text form of a query() answer, shared by railrun (which writes
// answer files), railbench (which hashes the engine's answers) and railjudge
// (which compares them with the reference answers).
//
// One line per query, without the newline:
//   <status>                                     for non-Ok statuses
//   0 <count> (| (<train> <day> <from> <to> <depart> <arrive>)*)*
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "ticket_system.h"

namespace railtools {

inline void append_answer(std::string& out, rail::Status st, const rail::QueryResult& r) {
  out += std::to_string(static_cast<int>(st));
  if (st != rail::Status::Ok) return;
  out += ' ';
  out += std::to_string(r.count);
  const uint32_t count = r.count < uint32_t(rail::kMaxQueryResults) ? r.count : uint32_t(rail::kMaxQueryResults);
  for (uint32_t i = 0; i < count; ++i) {
    const auto& it = r.items[i];
    out += " |";
    const uint32_t legs = it.leg_count < uint32_t(rail::kMaxLegs) ? it.leg_count : uint32_t(rail::kMaxLegs);
    for (uint32_t j = 0; j < legs; ++j) {
      const auto& l = it.legs[j];
      out += ' ' + std::to_string(l.train) + ' ' + std::to_string(l.day) + ' ' + std::to_string(l.from) + ' ' +
             std::to_string(l.to) + ' ' + std::to_string(l.depart) + ' ' + std::to_string(l.arrive);
    }
  }
}

inline uint64_t fnv1a(const char* data, size_t n) {
  uint64_t h = 0xcbf29ce484222325ull;
  for (size_t i = 0; i < n; ++i) {
    h ^= static_cast<unsigned char>(data[i]);
    h *= 0x100000001b3ull;
  }
  return h;
}

inline uint64_t answer_hash(rail::Status st, const rail::QueryResult& r) {
  thread_local std::string line;
  line.clear();
  append_answer(line, st, r);
  return fnv1a(line.data(), line.size());
}

// A parsed answer line: the itineraries only matter for booking requests.
struct Answer {
  uint64_t hash = 0;
  int status = 0;
  std::vector<rail::Itinerary> items;
};

// Parses one line written by append_answer. Returns false if malformed.
bool parse_answer(const std::string& line, Answer* out);

// Indices of the workload intents that issue a query, in file order (main
// phase first, then quiet). Reference answer files list answers in this order.
struct Workload;
std::vector<uint32_t> query_ordinals(const Workload& load, bool quiet);

}  // namespace railtools
