// Raw log written by railbench and read by railjudge.
//
// One Event per engine call. Times are nanoseconds since the start of the
// concurrent phase, from std::steady_clock. Successful books point at a
// SeatBlock with the seats the engine returned. The file is a native-endian
// dump: it is produced and consumed on the same machine.
//
// After the quiet phase comes the audit phase: for every leg of a failed
// multi-leg booking and of a refunded order, books of exactly that
// interval are repeated until SoldOut (see railbench). Seats the
// engine lost along the way then show up as exact false negatives.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "ticket_system.h"

namespace railtools {

enum class Op : uint8_t { Query = 0, Book = 1, Refund = 2 };
enum class Phase : uint8_t { Main = 0, Quiet = 1, Audit = 2 };

inline constexpr uint8_t kBadBookAttempt = 255;
inline constexpr uint32_t kNoSeats = UINT32_MAX;

struct Event {
  uint64_t start;   // ns
  uint32_t dur;     // ns, saturating
  uint32_t intent;  // index into the phase's intents; audit: index into BenchLog::probes
  uint64_t value;   // query: answer hash; book: order id; refund: order id
  uint32_t seats;   // book Ok: index of its SeatBlock, else kNoSeats
  Op op;
  uint8_t status;   // rail::Status
  uint8_t attempt;  // book: which itinerary of the purchase's query, or kBadBookAttempt;
                    // audit: number of passengers
  Phase phase;
};

// When the call returned, in ns. A saturated duration (a call slower than
// about 4.3 s, e.g. a thread starved on a lock) means "some time later":
// the judge then treats the call as possibly still running, which is the
// permissive reading for every check.
constexpr uint64_t kOpenEnd = UINT64_MAX / 2;
inline uint64_t event_end(const Event& e) { return e.dur == UINT32_MAX ? kOpenEnd : e.start + e.dur; }

static_assert(sizeof(Event) == 32, "Event layout");

struct SeatBlock {
  rail::Seat seats[rail::kMaxLegs][rail::kMaxPassengers];
};

// One audited interval: book `leg` in `seat_class` until SoldOut.
struct Probe {
  rail::LegRef leg;
  uint32_t seat_class;
};

struct BenchLog {
  uint32_t threads = 0;
  uint64_t seed = 0;
  uint64_t init_ns = 0;
  uint64_t main_ns = 0;    // wall time of the concurrent phase
  uint64_t quiet_ns = 0;   // wall time of the quiet phase
  uint64_t main_intents = 0, quiet_intents = 0;
  std::vector<Event> events;
  std::vector<SeatBlock> seat_blocks;
  std::vector<Probe> probes;
  uint64_t audit_ns = 0;
};

void write_bench_log(const std::string& path, const BenchLog& log);
BenchLog read_bench_log(const std::string& path);

}  // namespace railtools
