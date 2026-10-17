// ticket_system.h - interface between the railway benchmark harness and a
// contestant's ticketing engine. Read-only for contestants.
//
// The full semantics are specified in TASK.md; the comments here are a
// summary. When they disagree, TASK.md wins.
#pragma once

#include <cstdint>

namespace rail {

// ---------------------------------------------------------------- constants

inline constexpr int kDays = 3;                 // days on sale: 0, 1, 2
inline constexpr int kMinutesPerDay = 1440;
inline constexpr int kMinTransferMinutes = 30;
inline constexpr int kMaxLegs = 3;              // at most 2 transfers
inline constexpr int kMaxStopsPerTrain = 60;
inline constexpr int kMaxPassengers = 5;
inline constexpr int kMaxQueryResults = 5;

enum class SeatClass : uint8_t { Business = 0, First = 1, Second = 2 };
inline constexpr int kSeatClassCount = 3;

// Every train has the same 8-car composition (578 seats per train-day):
//   car 1     Business  rows 1..8   letters A | C F       24 seats
//   cars 2-3  First     rows 1..13  letters A C | D F     52 seats per car
//   cars 4-8  Second    rows 1..18  letters A B C | D F   90 seats per car
// Window seats are A and F. '|' marks the aisle.
inline constexpr int kCarCount = 8;
inline constexpr int kSeatsPerTrainDay = 578;

// ---------------------------------------------------------------- world data

// One stop of a train. Times are minutes after 00:00 of the day the train
// departs from its first stop, and may exceed 1440 for trains running past
// midnight. arrive == depart at the first and last stop; at intermediate stops
// arrive < depart. Times strictly increase along the train.
struct Stop {
  uint32_t station;
  int32_t arrive;
  int32_t depart;
};

struct Train {
  uint32_t line;         // the line the train starts on (informational: a cross-line
                         // train continues onto another line at a shared station)
  uint32_t stop_begin;   // index of the first stop in WorldData::stops
  uint32_t stop_count;   // 2..kMaxStopsPerTrain; a train visits a station at most once
  const char* name;      // e.g. "G123"; display only
};

struct Line {
  uint32_t station_begin;  // index into WorldData::line_stations
  uint32_t station_count;
  const char* name;        // display only
};

// All pointers stay valid and unchanged for the lifetime of the TicketSystem.
struct WorldData {
  uint32_t station_count;
  const char* const* station_names;  // [station_count], display only

  uint32_t line_count;
  const Line* lines;                 // [line_count]
  const uint32_t* line_stations;     // stations of every line, in order

  uint32_t train_count;
  const Train* trains;               // [train_count]
  const Stop* stops;                 // stops of every train, in order
};

// ---------------------------------------------------------------- requests

enum class Status : uint8_t {
  Ok = 0,
  SoldOut = 1,   // book: some leg lacks enough free seats of the class
  Invalid = 2,   // malformed request; nothing changed
  NotFound = 3,  // refund: unknown or already refunded order
};

// Absolute time = day * kMinutesPerDay + minutes of the train's timetable.

struct QueryRequest {
  uint32_t from;      // station
  uint32_t to;        // station
  int32_t earliest;   // absolute minute; first leg boards in [earliest, end of that day)
  uint32_t k;         // 1..kMaxQueryResults
};

struct Leg {
  uint32_t train;
  uint32_t day;       // departure day of the train-day, 0..kDays-1
  uint32_t from;      // boarding station
  uint32_t to;        // alighting station
  int32_t depart;     // absolute minute the train leaves `from`
  int32_t arrive;     // absolute minute the train reaches `to`
};

struct Itinerary {
  uint32_t leg_count;  // 1..kMaxLegs
  Leg legs[kMaxLegs];
};

struct QueryResult {
  uint32_t count;      // 0..k
  Itinerary items[kMaxQueryResults];
};

struct LegRef {
  uint32_t train;
  uint32_t day;
  uint32_t from;
  uint32_t to;
};

struct BookRequest {
  uint32_t leg_count;               // 1..kMaxLegs
  LegRef legs[kMaxLegs];
  SeatClass seat_class;
  uint32_t passenger_count;         // 1..kMaxPassengers
  bool window[kMaxPassengers];      // per passenger; soft preference
  bool together;                    // soft preference; ignored for one passenger
};

struct Seat {
  uint8_t car;    // 1..kCarCount
  uint8_t row;    // 1-based
  char letter;    // 'A', 'B', 'C', 'D' or 'F'
};

struct BookResult {
  uint64_t order_id;                       // nonzero, unique for the lifetime of the system
  Seat seats[kMaxLegs][kMaxPassengers];    // seats[leg][passenger]
};

// ---------------------------------------------------------------- engine

// init() is called exactly once, from a single thread, before anything else.
// After it returns, query(), book() and refund() may be called concurrently
// from any number of threads and must be linearizable. The harness may exit
// without destroying the object.
class TicketSystem {
 public:
  virtual ~TicketSystem() = default;

  virtual void init(const WorldData& world) = 0;

  // Returns Ok (possibly with zero results) or Invalid.
  virtual Status query(const QueryRequest& req, QueryResult* out) = 0;

  // Returns Ok, SoldOut or Invalid. `out` is only meaningful on Ok.
  virtual Status book(const BookRequest& req, BookResult* out) = 0;

  // Returns Ok or NotFound.
  virtual Status refund(uint64_t order_id) = 0;
};

// Implemented by the contestant. Called once by the harness.
TicketSystem* create_ticket_system();

}  // namespace rail
